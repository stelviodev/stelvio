// Darwin ABI adaptation only. Authorization and lifecycle policy live in Go.
#include "bindings.h"
#include <bsm/libbsm.h>
#include <libproc.h>
#include <launch.h>
#include <SystemConfiguration/SystemConfiguration.h>
#include <sys/acl.h>
#include <sys/socket.h>
#include <sys/sysctl.h>
#include <sys/kern_control.h>
#include <sys/sys_domain.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/stdio.h>
#include <net/if.h>
#include <net/if_dl.h>
#include <net/route.h>
#include <arpa/inet.h>
#include <fcntl.h>
#include <poll.h>
#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <stdbool.h>
#include <time.h>
static int empty_acl(acl_t acl) {
 if(!acl)return errno==ENOENT;
 acl_entry_t entry;int ok=0;if(!acl_valid(acl)){errno=0;ok=acl_get_entry(acl,ACL_FIRST_ENTRY,&entry)==-1&&errno==EINVAL;}if(acl_free(acl))ok=0;return ok;
}
int tf_no_acl(int fd){errno=0;return empty_acl(acl_get_fd_np(fd,ACL_TYPE_EXTENDED));}
int tf_no_acl_path(const char *path){errno=0;return empty_acl(acl_get_link_np(path,ACL_TYPE_EXTENDED));}
static int live_token(const uint32_t *bytes){audit_token_t token;memcpy(&token,bytes,sizeof(token));char path[PROC_PIDPATHINFO_MAXSIZE];return proc_pidpath_audittoken(&token,path,sizeof(path))>0;}
int tf_peer_read(int fd,tf_peer *p){
 memset(p,0,sizeof(*p));uid_t uid;gid_t gid;pid_t pid;socklen_t n=sizeof(pid),t=sizeof(p->token);audit_token_t token;struct proc_bsdinfo info;
 if(getpeereid(fd,&uid,&gid)||getsockopt(fd,SOL_LOCAL,LOCAL_PEERPID,&pid,&n)||n!=sizeof(pid)||getsockopt(fd,SOL_LOCAL,LOCAL_PEERTOKEN,&token,&t)||t!=sizeof(token))return -1;
 memcpy(p->token,&token,sizeof(token));
 if(!live_token(p->token)||proc_pidinfo(pid,PROC_PIDTBSDINFO,0,&info,sizeof(info))!=sizeof(info)||!live_token(p->token))return -1;
 if(audit_token_to_pid(token)!=pid||audit_token_to_euid(token)!=uid||audit_token_to_ruid(token)!=uid||info.pbi_uid!=uid||info.pbi_ruid!=uid||info.pbi_status==5)return -1;
 p->uid=uid;p->pid=pid;p->seconds=info.pbi_start_tvsec;p->micros=info.pbi_start_tvusec;return 0;
}
int tf_peer_live(const tf_peer *p){
 struct proc_bsdinfo info;if(!live_token(p->token)||proc_pidinfo(p->pid,PROC_PIDTBSDINFO,0,&info,sizeof(info))!=sizeof(info)||!live_token(p->token))return 0;
 return info.pbi_status!=5&&info.pbi_uid==p->uid&&info.pbi_ruid==p->uid&&info.pbi_start_tvsec==p->seconds&&info.pbi_start_tvusec==p->micros;
}
int tf_image(uint64_t *dev,uint64_t *ino,char *path,size_t capacity){
 struct proc_regionwithpathinfo loaded;
 if(proc_pidpath(getpid(),path,(uint32_t)capacity)<=0||proc_pidinfo(getpid(),PROC_PIDREGIONPATHINFO,(uint64_t)(uintptr_t)&tf_image,&loaded,sizeof(loaded))!=sizeof(loaded))return -1;
 *dev=(uint32_t)loaded.prp_vip.vip_vi.vi_stat.vst_dev;*ino=loaded.prp_vip.vip_vi.vi_stat.vst_ino;return 0;
}
int tf_listener(void){int *fds=NULL;size_t n=0;if(launch_activate_socket("Control",&fds,&n))return -1;int fd=n==1?fds[0]:-1;for(size_t i=0;i<n;i++)if(fds[i]!=fd)close(fds[i]);free(fds);return fd;}
int tf_utun(char *name,size_t capacity){
 int fd=socket(PF_SYSTEM,SOCK_DGRAM,SYSPROTO_CONTROL);if(fd<0)return -1;
 struct ctl_info ctl={0};strlcpy(ctl.ctl_name,"com.apple.net.utun_control",sizeof(ctl.ctl_name));
 struct sockaddr_ctl addr={.sc_len=sizeof(addr),.sc_family=AF_SYSTEM,.ss_sysaddr=AF_SYS_CONTROL,.sc_unit=0};
 if(ioctl(fd,CTLIOCGINFO,&ctl))goto fail;addr.sc_id=ctl.ctl_id;
 if(connect(fd,(struct sockaddr*)&addr,sizeof(addr)))goto fail;socklen_t n=(socklen_t)capacity;
 if(getsockopt(fd,SYSPROTO_CONTROL,2,name,&n)||n<2||n>capacity||name[n-1])goto fail;return fd;
 fail:close(fd);return -1;
}
int tf_if_current(int fd,const char *name,uint32_t index){char current[IFNAMSIZ]={0};socklen_t n=sizeof(current);return !getsockopt(fd,SYSPROTO_CONTROL,2,current,&n)&&n>1&&n<=sizeof(current)&&!current[n-1]&&!strcmp(current,name)&&if_nametoindex(name)==index;}
static struct sockaddr_in ip(uint32_t value){struct sockaddr_in a={.sin_len=sizeof(a),.sin_family=AF_INET};a.sin_addr.s_addr=htonl(value);return a;}
int tf_configure(const char *name,uint32_t local,uint32_t peer){
 int fd=socket(AF_INET,SOCK_DGRAM,0);if(fd<0)return -1;struct ifaliasreq alias={0};strlcpy(alias.ifra_name,name,sizeof(alias.ifra_name));struct sockaddr_in a=ip(local),b=ip(peer),mask=ip(UINT32_MAX);
 memcpy(&alias.ifra_addr,&a,sizeof(a));memcpy(&alias.ifra_broadaddr,&b,sizeof(b));memcpy(&alias.ifra_mask,&mask,sizeof(mask));
 int ok=!ioctl(fd,SIOCAIFADDR,&alias);struct ifreq flags={0};strlcpy(flags.ifr_name,name,sizeof(flags.ifr_name));if(ok)ok=!ioctl(fd,SIOCGIFFLAGS,&flags);flags.ifr_flags|=IFF_UP;if(ok)ok=!ioctl(fd,SIOCSIFFLAGS,&flags);if(close(fd))ok=0;return ok?0:-1;
}
int tf_route_add(uint32_t index,uint32_t network,uint32_t mask){
 int fd=socket(PF_ROUTE,SOCK_RAW,0);if(fd<0)return -1;
 struct {struct rt_msghdr h;struct sockaddr_in dst;struct sockaddr_dl gateway;struct sockaddr_in mask;} m={0};
 m.h.rtm_msglen=sizeof(m);m.h.rtm_version=RTM_VERSION;m.h.rtm_type=RTM_ADD;m.h.rtm_flags=RTF_UP|RTF_STATIC;m.h.rtm_addrs=RTA_DST|RTA_GATEWAY|RTA_NETMASK;m.h.rtm_index=(unsigned short)index;m.h.rtm_pid=getpid();m.h.rtm_seq=1;
 m.dst=ip(network);m.mask=ip(mask);m.gateway.sdl_len=sizeof(m.gateway);m.gateway.sdl_family=AF_LINK;m.gateway.sdl_index=(unsigned short)index;
 int ok=0;if(index>UINT16_MAX||send(fd,&m,sizeof(m),MSG_DONTWAIT)!=sizeof(m))goto done;
 ok=tf_route_ack(fd,getpid(),1);
 done:if(close(fd))ok=0;return ok?0:-1;
}
int tf_routes(tf_route *out,size_t capacity,size_t *count){
 int mib[]={CTL_NET,PF_ROUTE,0,AF_INET,NET_RT_DUMP2,0};unsigned char *table=NULL;size_t size=0;*count=0;
 for(int i=0;i<4;i++){if(sysctl(mib,6,NULL,&size,NULL,0)||size>16*1024*1024)return -1;if(!size)return 0;size_t cap=size;table=malloc(cap);if(!table)return -1;if(!sysctl(mib,6,table,&size,NULL,0)){if(size>cap){free(table);return -1;}break;}int e=errno;free(table);table=NULL;if(e!=ENOMEM)return -1;}
 if(!table)return -1;int result=-1;
 for(size_t offset=0;offset<size;){if(size-offset<sizeof(struct rt_msghdr2))goto done;struct rt_msghdr2 m;memcpy(&m,table+offset,sizeof(m));if(m.rtm_msglen<sizeof(m)||m.rtm_msglen>size-offset||m.rtm_version!=RTM_VERSION)goto done;
  tf_route route={.index=m.rtm_index};int ipv4=0;size_t pos=sizeof(m);
  for(unsigned i=0;i<RTAX_MAX;i++){if(!(m.rtm_addrs&(1<<i)))continue;if(m.rtm_msglen-pos<2)goto done;unsigned char *a=table+offset+pos;size_t len=a[0],padded=len?(len+3)&~3:4;if(padded>m.rtm_msglen-pos||(len&&len<2))goto done;struct sockaddr_in addr={0};memcpy(&addr,a,len<sizeof(addr)?len:sizeof(addr));
   if(i==RTAX_DST&&a[1]==AF_INET){if(len<8)goto done;ipv4=1;route.network=ntohl(addr.sin_addr.s_addr);}if(i==RTAX_NETMASK)route.mask=ntohl(addr.sin_addr.s_addr);pos+=padded;
  }
  if(m.rtm_flags&RTF_HOST)route.mask=UINT32_MAX;if(ipv4){if(*count>=capacity)goto done;out[(*count)++]=route;}offset+=m.rtm_msglen;
 }result=0;
 done:free(table);return result;
}
static int append_domain(CFTypeRef value,char *buf,size_t cap,size_t *size){char name[1024];if(CFGetTypeID(value)!=CFStringGetTypeID()||!CFStringGetCString(value,name,sizeof(name),kCFStringEncodingUTF8))return -1;size_t n=strlen(name);if(!n)return 0;if(n+1>cap-*size)return -1;memcpy(buf+*size,name,n+1);*size+=n+1;return 0;}
int tf_dns(char *buf,size_t cap,size_t *size){
 *size=0;SCDynamicStoreRef store=SCDynamicStoreCreate(NULL,CFSTR("Traforo DNS inventory"),NULL,NULL);if(!store)return -1;
 CFArrayRef keys=SCDynamicStoreCopyKeyList(store,CFSTR("^(State|Setup):/Network/(Global|Service/[^/]+)/DNS$"));int result=-1;if(!keys||CFArrayGetCount(keys)>4096)goto done;
 result=0;for(CFIndex i=0;i<CFArrayGetCount(keys)&&!result;i++){CFTypeRef key=CFArrayGetValueAtIndex(keys,i);if(CFGetTypeID(key)!=CFStringGetTypeID()){result=-1;break;}CFPropertyListRef value=SCDynamicStoreCopyValue(store,key);if(!value){result=-1;break;}
 if(CFGetTypeID(value)!=CFDictionaryGetTypeID())result=-1;else {CFTypeRef one=CFDictionaryGetValue(value,kSCPropNetDNSDomainName);if(one)result=append_domain(one,buf,cap,size);CFTypeRef many=CFDictionaryGetValue(value,kSCPropNetDNSSupplementalMatchDomains);if(many){if(CFGetTypeID(many)!=CFArrayGetTypeID()||CFArrayGetCount(many)>4096)result=-1;else for(CFIndex j=0;j<CFArrayGetCount(many)&&!result;j++)result=append_domain(CFArrayGetValueAtIndex(many,j),buf,cap,size);}}
 CFRelease(value);}
 done:if(keys)CFRelease(keys);CFRelease(store);return result;
}
int tf_rename_excl(int from,const char *source,int to,const char *target){return renameatx_np(from,source,to,target,RENAME_EXCL);}

static uint64_t milliseconds(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now)) return 0;
    return (uint64_t)now.tv_sec * 1000 + (uint64_t)now.tv_nsec / 1000000;
}

int tf_route_ack(int descriptor, pid_t pid, int sequence) {
    uint64_t began = milliseconds();
    if (!began) return false;
    while (true) {
        uint64_t now = milliseconds();
        if (!now || now >= began + 3000) return false;
        struct pollfd wait = {.fd=descriptor, .events=POLLIN};
        int ready = poll(&wait, 1, (int)(began + 3000 - now));
        if (ready < 0 && errno == EINTR) continue;
        if (ready <= 0 || !(wait.revents & POLLIN)) return false;
        uint8_t reply[4096];
        ssize_t count = recv(descriptor, reply, sizeof(reply), MSG_DONTWAIT);
        if (count < 0 && (errno == EINTR || errno == EAGAIN)) continue;
        /* Interface/address notifications have a shorter header than RTM_ADD.
         * Validate their common envelope before selecting the ADD response. */
        if (count < 4) return false;
        uint16_t length;
        memcpy(&length, reply, sizeof(length));
        if (length != count || reply[2] != RTM_VERSION) return false;
        if (reply[3] != RTM_ADD) continue;
        if (count < (ssize_t)sizeof(struct rt_msghdr)) return false;
        struct rt_msghdr header;
        memcpy(&header, reply, sizeof(header));
        if (header.rtm_pid != pid || header.rtm_seq != sequence) continue;
        return !header.rtm_errno;
    }
}
