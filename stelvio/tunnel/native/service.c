/* Global root service. One native mutation worker; the main thread owns peer
 * admission, EOF leases and packet forwarding. No subprocess/SSH/TCP stack. */
#include "service.h"
#include "acl.h"
#include "io.h"
#include "journal.h"
#include "packet_io.h"
#include "pump.h"
#include "state_store.h"
#include "unit.h"
#include <errno.h>
#include <fcntl.h>
#include <launch.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#define CLIENTS 8
struct client { int descriptor; uint64_t deadline; struct stlv_peer peer; struct stlv_input input; };
struct grant { bool active, failed; uint8_t phase; uint64_t generation;
               struct stlv_request configuration; };
enum job_kind { CONFIGURE, REMOVE, CLEAN, RESET };
struct service;
struct job {
    enum job_kind kind;
    int connection, result;
    bool cache_error;
    struct stlv_request request;
    struct stlv_peer peer;
    struct stlv_input input;
    struct service *service;
};
struct service {
    int image, lock, listener, lease, carrier, completion[2];
    bool owned, uncertain, working, revoke, stopping;
    uint64_t stop_deadline, cleanup_after;
    pthread_t thread;
    struct stlv_peer peer;
    uint8_t session[16], capability[16];
    struct stlv_snapshot state;
    struct stlv_interface interfaces[STLV_MAX_UNITS];
    struct grant grants[STLV_MAX_UNITS];
    struct client clients[CLIENTS];
    struct job job;
};
static volatile sig_atomic_t stopping;
static void stop_signal(int number) { (void)number; stopping = 1; }
static uint64_t now(void) {
    struct timespec value;
    if (clock_gettime(CLOCK_MONOTONIC, &value)) return 0;
    return (uint64_t)value.tv_sec * 1000 + (uint64_t)value.tv_nsec / 1000000;
}
static bool same_peer(const struct stlv_peer *a, const struct stlv_peer *b) {
    return a->uid == b->uid && a->pid == b->pid && a->birth_seconds == b->birth_seconds &&
        a->birth_microseconds == b->birth_microseconds && !memcmp(&a->token, &b->token, sizeof(a->token));
}
static void close_client(struct client *client) {
    if (client->descriptor >= 0) close(client->descriptor);
    memset(client, 0, sizeof(*client));
    client->descriptor = -1;
}
static void reply_close(int descriptor, uint16_t status) {
    stlv_reply_once(descriptor, status, NULL, 0, -1);
    close(descriptor);
}
static bool nonblocking(int descriptor) {
    int flags = fcntl(descriptor, F_GETFL);
    return flags >= 0 && !fcntl(descriptor, F_SETFL, flags | O_NONBLOCK) &&
        !fcntl(descriptor, F_SETFD, FD_CLOEXEC);
}
static int listener(void) {
    int *descriptors = NULL;
    size_t count = 0;
    if (launch_activate_socket(STLV_LAUNCH_SOCKET, &descriptors, &count)) return -1;
    int result = -1;
    if (count == 1) {
        int descriptor = descriptors[0], type, accepting;
        socklen_t length = sizeof(type), address_size = sizeof(struct sockaddr_un);
        struct sockaddr_un address = {0};
        struct stat path;
        bool valid = !getsockopt(descriptor, SOL_SOCKET, SO_TYPE, &type, &length) && type == SOCK_STREAM;
        length = sizeof(accepting);
        valid = valid && !getsockopt(descriptor, SOL_SOCKET, SO_ACCEPTCONN, &accepting, &length) && accepting &&
            !getsockname(descriptor, (struct sockaddr *)&address, &address_size) &&
            address.sun_family == AF_UNIX && memchr(address.sun_path, 0, sizeof(address.sun_path)) &&
            !strcmp(address.sun_path, STLV_SOCKET) && !lstat(STLV_SOCKET, &path) &&
            S_ISSOCK(path.st_mode) && !path.st_uid && stlv_no_acl_path(STLV_SOCKET) && (path.st_mode & 0777) == 0666 && nonblocking(descriptor);
        if (valid) result = descriptor;
    }
    for (size_t i = 0; i < count; i++) if (descriptors[i] != result) close(descriptors[i]);
    free(descriptors);
    return result;
}
static void revoke_lease(struct service *service) {
    if (service->lease >= 0) close(service->lease);
    if (service->carrier >= 0) close(service->carrier);
    service->lease = service->carrier = -1;
    for (uint8_t i = 0; i < STLV_MAX_UNITS; i++) service->grants[i].active = false;
    service->revoke = true;
}
static int cleanup(struct service *service) {
    /* The live descriptors themselves remain authoritative even when durable
     * records cannot be read. Revoke them independently; retain DNS evidence. */
    bool closed = true;
    for (uint8_t i = 0; i < STLV_MAX_UNITS; i++)
        if (!stlv_interface_close(&service->interfaces[i])) closed = false;
    int loaded = stlv_state_load(service->image, service->lock, &service->state);
    if (loaded <= 0) return loaded < 0 || !closed ? -1 : 0;
    int result = closed ? 0 : -1;
    for (uint8_t i = 0; i < service->state.unit_count; i++) {
        struct stlv_unit_snapshot *unit = &service->state.units[i];
        if (unit->phase == STLV_REMOVED) continue;
        uint64_t generation = unit->generation;
        if (unit->phase != STLV_REMOVING || unit->keep_dns) {
            if (generation == UINT64_MAX) { result = -1; continue; }
            generation++;
        }
        if (stlv_unit_remove(service->image, service->lock, &service->state, service->interfaces,
                             unit->configuration.unit, generation, false)) {
            result = -1;
            /* Close only the locally retained kernel FD even when file/journal
             * recovery is uncertain. Never use saved indices for mutations. */
            stlv_interface_close(&service->interfaces[i]);
            if (stlv_state_load(service->image, service->lock, &service->state) != 1) return -1;
        }
    }
    if (result) return result;
    for (uint8_t i = 0; i < STLV_MAX_UNITS; i++)
        if (service->interfaces[i].descriptor >= 0) return -1;
    if (!stlv_state_current(service->image, service->lock, &service->state) ||
        stlv_journal_remove(service->image, service->lock)) return -1;
    memset(&service->state, 0, sizeof(service->state));
    return 0;
}
static void *mutate(void *argument) {
    struct job *job = argument;
    struct service *service = job->service;
    if (job->kind == CONFIGURE)
        job->result = stlv_unit_configure(service->image, service->lock, &service->state,
            service->interfaces, job->input.packet, job->input.size);
    else if (job->kind == REMOVE)
        job->result = stlv_unit_remove(service->image, service->lock, &service->state,
            service->interfaces, job->request.unit, job->request.generation, job->request.keep_dns);
    else job->result = cleanup(service);
    /* A save may have committed before reporting uncertain fsync/close. Never
     * retain a stale cached snapshot after that failure. */
    if (job->result && stlv_state_load(service->image, service->lock, &service->state) <= 0) {
        job->result = -1; job->cache_error = true;
    }
    char byte = 1;
    ssize_t sent;
    do { sent = write(service->completion[1], &byte, 1); } while (sent < 0 && errno == EINTR);
    if (sent != 1) _exit(1); /* Root FDs close; durable evidence stays for restart. */
    return NULL;
}
static bool start_job(struct service *service, enum job_kind kind, struct client *client,
                       const struct stlv_request *request) {
    if (service->working) return false;
    memset(&service->job, 0, sizeof(service->job));
    service->job.kind = kind;
    service->job.connection = client ? client->descriptor : -1;
    service->job.service = service;
    if (client) { service->job.peer = client->peer; service->job.input = client->input; }
    if (request) service->job.request = *request;
    if (kind == CONFIGURE || kind == REMOVE) {
        for (uint8_t i = 0; i < STLV_MAX_UNITS; i++)
            if (service->grants[i].configuration.unit == request->unit) service->grants[i].active = false;
    } else for (uint8_t i = 0; i < STLV_MAX_UNITS; i++) service->grants[i].active = false;
    if (pthread_create(&service->thread, NULL, mutate, &service->job)) return false;
    service->working = true;
    if (client) { client->descriptor = -1; close_client(client); }
    return true;
}
static void grants(struct service *service) {
    for (uint8_t i = 0; i < STLV_MAX_UNITS; i++) {
        service->grants[i].active = false;
        service->grants[i].phase = 0;
    }
    for (uint8_t i = 0; i < service->state.unit_count; i++) {
        struct grant *grant = &service->grants[i];
        grant->configuration = service->state.units[i].configuration;
        grant->phase = service->state.units[i].phase;
        grant->generation = service->state.units[i].generation;
        if (service->revoke || service->lease < 0 || service->uncertain || grant->failed ||
            grant->phase != STLV_ACTIVE || !stlv_interface_current(&service->interfaces[i])) continue;
        grant->active = true;
    }
}

static void acquire(struct service *service, struct client *client, const struct stlv_request *request) {
    struct stlv_snapshot *state = &service->state;
    int pair[2] = {-1, -1};
    if (!stlv_peer_alive(&client->peer) || !stlv_packet_pair(pair)) {
        reply_close(client->descriptor, STLV_UNCERTAIN); client->descriptor = -1; return;
    }
    memset(state, 0, sizeof(*state));
    state->revision = 1; state->carrier_version = 1; state->peer = client->peer;
    memcpy(state->session, request->session, 16);
    arc4random_buf(state->capability, sizeof(state->capability));
    if (stlv_state_save(service->image, service->lock, state)) {
        close(pair[0]); close(pair[1]);
        service->uncertain = true;
        /* Retain any committed owner after an uncertain response, so no other
         * process can acquire until reconciliation certifies disposal. */
        if (stlv_state_load(service->image, service->lock, state) == 1) {
            service->owned = true; service->peer = state->peer;
            memcpy(service->session, state->session, 16); memcpy(service->capability, state->capability, 16);
        }
        reply_close(client->descriptor, STLV_UNCERTAIN); client->descriptor = -1; return;
    }
    service->owned = true; service->uncertain = false; service->revoke = false;
    service->peer = state->peer;
    memcpy(service->session, state->session, 16); memcpy(service->capability, state->capability, 16);
    service->carrier = pair[0];
    memset(service->grants, 0, sizeof(service->grants));
    uint8_t reply[20]; memcpy(reply, state->capability, 16);
    reply[16] = reply[17] = reply[18] = 0; reply[19] = 1; /* carrier version */
    bool delivered = stlv_reply_once(client->descriptor, STLV_OK, reply, sizeof(reply), pair[1]);
    memset(reply, 0, sizeof(reply)); close(pair[1]);
    service->lease = client->descriptor; client->descriptor = -1;
    if (!delivered) revoke_lease(service);
}
static void complete(struct service *service) {
    char byte;
    if (read(service->completion[0], &byte, 1) != 1 || pthread_join(service->thread, NULL)) _exit(1);
    service->working = false;
    struct job *job = &service->job;
    bool disposal = job->kind == CLEAN || job->kind == RESET;
    if (job->cache_error) { service->uncertain = true; revoke_lease(service); }
    if (!disposal) for (uint8_t i = 0; i < service->state.unit_count; i++)
        if (service->state.units[i].configuration.unit == job->request.unit)
            service->grants[i].failed = job->result != 0;
    if (disposal && !job->result) {
        service->owned = false; service->uncertain = false;
        memset(&service->peer, 0, sizeof(service->peer));
        memset(service->session, 0, 16); memset(service->capability, 0, 16);
    } else if (disposal) {
        service->uncertain = true;
        service->cleanup_after = now() + 1000;
    }
    if (job->connection >= 0) {
        if (job->kind == RESET && !job->result && !service->stopping && !service->revoke) {
            struct client client = {.descriptor=job->connection, .peer=job->peer};
            struct stlv_peer peer;
            if (stlv_peer_read(client.descriptor, &peer) && same_peer(&peer, &client.peer))
                acquire(service, &client, &job->request);
            else reply_close(client.descriptor, STLV_UNAUTHORIZED);
        } else reply_close(job->connection, job->result ? STLV_UNCERTAIN : STLV_OK);
    }
    memset(job, 0, sizeof(*job)); job->connection = -1;
    grants(service);
}
static void handle(struct service *service, struct client *client, const struct stlv_request *request) {
    if (request->operation == STLV_INSPECT) {
        uint8_t reply[21 + 13 * STLV_MAX_UNITS] = "STLVHLP1";
        reply[11] = STLV_HELPER_ABI; reply[15] = 1;
        reply[19] = (service->owned ? 1 : 0) | (service->uncertain ? 2 : 0) | (service->working ? 4 : 0);
        size_t size = 21;
        for (uint8_t i = 0; i < STLV_MAX_UNITS; i++) {
            const struct grant *grant = &service->grants[i];
            if (!grant->phase) continue;
            reply[20]++;
            uint32_t identity = grant->configuration.unit;
            for (unsigned byte = 0; byte < 4; byte++) reply[size + 3 - byte] = (uint8_t)(identity >> (8 * byte));
            for (unsigned byte = 0; byte < 8; byte++) reply[size + 11 - byte] = (uint8_t)(grant->generation >> (8 * byte));
            reply[size + 12] = grant->active ? 1 : grant->failed ? 2 :
                grant->phase == STLV_PREPARING || grant->phase == STLV_REMOVING ? 3 :
                grant->phase == STLV_RETAINED ? 4 : 0;
            size += 13;
        }
        stlv_reply_once(client->descriptor, STLV_OK, reply, size, -1); close_client(client); return;
    }
    if (service->working || service->stopping) { reply_close(client->descriptor, STLV_BUSY); client->descriptor = -1; return; }
    if (request->operation == STLV_ACQUIRE) {
        if (service->lease >= 0 || (service->owned && stlv_peer_alive(&service->peer) &&
            (!same_peer(&client->peer, &service->peer) || memcmp(request->session, service->session, 16)))) {
            reply_close(client->descriptor, STLV_BUSY); client->descriptor = -1; return;
        }
        if (service->owned || service->uncertain) {
            service->revoke = false;
            if (!start_job(service, RESET, client, request)) close_client(client);
        } else acquire(service, client, request);
        return;
    }
    if (request->operation == STLV_RECONCILE) {
        if (service->lease >= 0 || (service->owned && stlv_peer_alive(&service->peer))) {
            reply_close(client->descriptor, STLV_BUSY); client->descriptor = -1; return;
        }
        if (!start_job(service, CLEAN, client, request)) close_client(client);
        return;
    }
    if (!service->owned || service->lease < 0 || service->revoke ||
        !same_peer(&client->peer, &service->peer) || memcmp(request->session, service->session, 16) ||
        memcmp(request->capability, service->capability, 16)) {
        reply_close(client->descriptor, STLV_UNAUTHORIZED); client->descriptor = -1; return;
    }
    enum job_kind kind = request->operation == STLV_CONFIGURE ? CONFIGURE :
        request->operation == STLV_REMOVE ? REMOVE : CLEAN;
    if (request->operation == STLV_RELEASE) revoke_lease(service);
    if (!start_job(service, kind, client, request)) close_client(client);
}
static void accept_client(struct service *service) {
    int connection = accept(service->listener, NULL, NULL);
    if (connection < 0) return;
    struct stlv_peer peer;
    if (!nonblocking(connection) || !stlv_peer_read(connection, &peer)) { close(connection); return; }
    /* Foreign active callers are refused before reading any frame, so a slow
     * requester cannot occupy control slots or interfere with the owner. */
    if (service->owned && (service->lease >= 0 || stlv_peer_alive(&service->peer)) &&
        !same_peer(&peer, &service->peer)) { reply_close(connection, STLV_BUSY); return; }
    for (size_t i = 0; i < CLIENTS; i++) if (service->clients[i].descriptor < 0) {
        service->clients[i].descriptor = connection; service->clients[i].peer = peer;
        service->clients[i].deadline = now() + 10000; return;
    }
    reply_close(connection, STLV_BUSY);
}
static void lease_event(struct service *service) {
    char byte;
    union { struct cmsghdr aligned; uint8_t bytes[STLV_CONTROL_BYTES]; } control = {0};
    struct iovec vector = {.iov_base=&byte, .iov_len=1};
    struct msghdr message = {.msg_iov=&vector, .msg_iovlen=1,
        .msg_control=control.bytes, .msg_controllen=sizeof(control.bytes)};
    ssize_t count = recvmsg(service->lease, &message, MSG_DONTWAIT);
    if (count < 0 && (errno == EAGAIN || errno == EINTR)) return;
    if (count >= 0) stlv_ancillary_free(&message, control.bytes, sizeof(control.bytes));
    revoke_lease(service); /* EOF or unexpected input terminates this lease. */
}
int stlv_service(void) {
    if (!stlv_trusted_image() || !stlv_io_supported()) return 1;
    umask(077);
    struct service *service = calloc(1, sizeof(*service));
    if (!service) return 1;
    service->image = service->lock = service->listener = service->lease = service->carrier = -1;
    service->completion[0] = service->completion[1] = -1;
    for (size_t i = 0; i < CLIENTS; i++) service->clients[i].descriptor = -1;
    for (size_t i = 0; i < STLV_MAX_UNITS; i++) service->interfaces[i].descriptor = -1;
    service->image = stlv_image_lock(); service->lock = stlv_state_lock();
    if (service->image < 0 || service->lock < 0 || pipe(service->completion) ||
        !nonblocking(service->completion[0]) || !nonblocking(service->completion[1]) ||
        (service->listener = listener()) < 0) return 1;
    int loaded = stlv_state_load(service->image, service->lock, &service->state);
    if (loaded < 0) service->uncertain = true;
    if (loaded == 1) {
        service->owned = true; service->peer = service->state.peer;
        memcpy(service->session, service->state.session, 16); memcpy(service->capability, service->state.capability, 16);
    }
    grants(service); /* Read-only cache of any stale units; no kernel FD adoption. */
    struct sigaction action = {.sa_handler=stop_signal}; sigemptyset(&action.sa_mask);
    if (sigaction(SIGTERM, &action, NULL) || sigaction(SIGINT, &action, NULL)) return 1;
    for (;;) {
        uint64_t time = now();
        if (!time) _exit(1);
        if (stopping && !service->stopping) {
            service->stopping = true; service->stop_deadline = time + 30000; revoke_lease(service);
            for (size_t i = 0; i < CLIENTS; i++) close_client(&service->clients[i]);
        }
        if (service->stopping && time >= service->stop_deadline) _exit(1);
        if (service->lease >= 0 && !stlv_peer_alive(&service->peer)) revoke_lease(service);
        if (!service->working && (service->revoke || (service->owned && time >= service->cleanup_after && !stlv_peer_alive(&service->peer)))) {
            if (!start_job(service, CLEAN, NULL, NULL)) _exit(1);
            service->revoke = false;
        }
        if (service->stopping && !service->working && !service->owned && !service->uncertain) break;
        struct pollfd descriptors[4 + CLIENTS + STLV_MAX_UNITS];
        descriptors[0] = (struct pollfd){.fd=service->stopping ? -1 : service->listener, .events=POLLIN};
        descriptors[1] = (struct pollfd){.fd=service->lease, .events=POLLIN};
        descriptors[2] = (struct pollfd){.fd=service->carrier, .events=POLLIN};
        descriptors[3] = (struct pollfd){.fd=service->completion[0], .events=POLLIN};
        for (size_t i = 0; i < CLIENTS; i++) descriptors[4+i] = (struct pollfd){.fd=service->clients[i].descriptor, .events=POLLIN};
        for (size_t i = 0; i < STLV_MAX_UNITS; i++) descriptors[4+CLIENTS+i] = (struct pollfd){
            .fd=service->grants[i].active ? service->interfaces[i].descriptor : -1, .events=POLLIN};
        int ready = poll(descriptors, sizeof(descriptors)/sizeof(descriptors[0]), 50);
        if (ready < 0) { if (errno == EINTR) continue; _exit(1); }
        if (descriptors[1].revents) lease_event(service);
        if (descriptors[3].revents) complete(service);
        if (descriptors[0].revents && !service->stopping) accept_client(service);
        for (size_t i = 0; i < CLIENTS; i++) {
            struct client *client = &service->clients[i];
            if (client->descriptor < 0) continue;
            uint64_t current = now();
            if (!current || current >= client->deadline) { close_client(client); continue; }
            if (!descriptors[4+i].revents) continue;
            struct stlv_request request;
            int received = stlv_receive_step(client->descriptor, &client->input, &request);
            if (received < 0) close_client(client);
            else if (received == 1) {
                current = now();
                if (!current || current >= client->deadline) close_client(client);
                else handle(service, client, &request);
            }
        }
        if (service->carrier >= 0 && descriptors[2].revents) {
            struct stlv_packet_frame frame;
            int received = stlv_packet_receive(service->carrier, &frame);
            if (received < 0) revoke_lease(service);
            else if (received) for (uint8_t i = 0; i < STLV_MAX_UNITS; i++) {
                if (!service->grants[i].active || service->grants[i].configuration.unit != frame.unit) continue;
                if (stlv_pump_return(&frame, &service->interfaces[i], &service->grants[i].configuration,
                    0xc0000201u + 2u*i) == STLV_PUMP_ERROR) {
                    service->grants[i].active = false; service->grants[i].failed = true;
                }
                break;
            }
        }
        for (uint8_t i = 0; i < STLV_MAX_UNITS; i++)
            if (service->grants[i].active && descriptors[4+CLIENTS+i].revents &&
                stlv_pump_host(&service->interfaces[i], &service->grants[i].configuration,
                    0xc0000201u + 2u*i, service->carrier) == STLV_PUMP_ERROR) {
                    service->grants[i].active = false; service->grants[i].failed = true;
                }
    }
    close(service->listener); close(service->completion[0]); close(service->completion[1]);
    close(service->lock); close(service->image); free(service);
    return 0;
}
