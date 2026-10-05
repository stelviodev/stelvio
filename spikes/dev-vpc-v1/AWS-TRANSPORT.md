# SSH and private DNS engineering proof

This fixture extends [the ownership proof](AWS-OWNERSHIP.md). It is not the
finished dev-mode feature. It creates billable resources; use an explicitly
authorized AWS account/profile/region, a fresh UUID, and destroy every attempt.

The app stack owns two customized VPCs, target security groups, and private
Route53 zones. The independent access stack owns two AL2023 arm64 t4g.nano
instances, encrypted root volumes, security groups and standalone rules, an
EC2 role/profile, and a fixed SSM identity document. Instances accept no public
SSH. Their dedicated nonroot user permits TCP forwarding with no sessions,
passwords, PTYs, agent forwarding, or sudo. A private echo service provides a
1 MiB TCP check; this fixture does not contain DocumentDB.

`ssh_transport.py` verifies instance ownership, absence of SSH ingress, SSM
document contents/ownership, readiness, and the server Ed25519 key through AWS.
It uploads an ephemeral client key with EC2 Instance Connect, rejects an
incorrect server pin, then starts OpenSSH over `AWS-StartSSHSession`. Pins are
retained for comparison on subsequent starts; private client keys are deleted
after the SSH/proxy groups stop. Never print plugin process arguments: they
contain session tokens.

`dns_relay.py` serves loopback UDP/TCP on ports 10890/10891. Each serves only its
UUID-qualified private suffix and forwards TCP DNS over the corresponding SOCKS
path to that VPC's resolver at its base address plus two. Upstream failure returns
SERVFAIL; other suffixes receive REFUSED. Request concurrency and waits are bounded.

## Provision and connect

Use the initialization/recovery prerequisites in AWS-OWNERSHIP.md. Resolve and
record a normal Amazon-owned AL2023 arm64 AMI in the chosen region; do not use a
minimal image. Set proof-specific shell variables:

```sh
PROOF_OWNER=<fresh-lowercase-uuid>
PROOF_BUCKET=<existing-awshome-bucket>
PROOF_REGION=<region>
PROOF_PROFILE=<authorized-profile>
PROOF_AMI=<pinned-normal-al2023-arm64-ami>
PROOF_OUT=spikes/dev-vpc-v1/build/aws-transport/$PROOF_OWNER
mkdir -p "$PROOF_OUT"
```

Run each phase separately, preserving logs and the owner metadata on any failure:

```sh
for phase in init app-up access-up; do
  .venv/bin/python spikes/dev-vpc-v1/aws_transport.py "$phase" \
    --owner "$PROOF_OWNER" --bucket "$PROOF_BUCKET" \
    --region "$PROOF_REGION" --profile "$PROOF_PROFILE" --ami "$PROOF_AMI" || break
done
.venv/bin/python spikes/dev-vpc-v1/aws_transport.py manifest \
  --owner "$PROOF_OWNER" --bucket "$PROOF_BUCKET" \
  --region "$PROOF_REGION" --profile "$PROOF_PROFILE" \
  --manifest-out "$PROOF_OUT/manifest.json"
.venv/bin/python spikes/dev-vpc-v1/ssh_transport.py \
  --manifest "$PROOF_OUT/manifest.json" --out "$PROOF_OUT/ssh"
```

Wait for authenticated readiness. In another terminal launch the nonroot DNS
relay, then the forwarder. Install the reviewed native broker only as described
in README.md, refusing an existing artifact and checking the installed hash.

```sh
.venv/bin/python spikes/dev-vpc-v1/dns_relay.py --manifest "$PROOF_OUT/manifest.json"
spikes/dev-vpc-v1/build/forwarder --config "$PROOF_OUT/ssh/forwarder.json"
sudo /Library/PrivilegedHelperTools/dev.stelvio.vpc-proof \
  --serve 10.254.0.0/16 10.253.0.0/16 --dns-owner "$PROOF_OWNER"
```

The optional DNS argument accepts only this fixed two-VPC proof profile. It
creates exactly two UUID-derived files in the preexisting root-owned
`/private/etc/resolver` directory. Existing files are never overwritten. A
root-owned journal records inode identities before contents; cleanup refuses a
changed inode/content or ambiguous unrecorded file. Root executes no Python,
AWS, SSH, or DNS forwarding code.

After READY, verify `socket.getaddrinfo()` for
`service.vpc0.<UUID>.stelvio-proof.test` and the corresponding `vpc1` name returns
10.254.0.10 and 10.253.0.10. Feed these original hostnames into `probe.py`'s
`probe()` callable for exact 1 MiB echo/half-close checks. Query both loopback
DNS listeners over UDP and TCP; check unrelated public DNS. Direct DNS queries
alone do not prove OS integration. Record per-check timestamps and do not run
checks after teardown or a fixture deadline.

For a one-path outage, suspend only the positively identified owned SSH group
and always resume it in `finally`. Wait beyond the positive record TTL before
the OS lookup. Verify SERVFAIL, OS lookup rejection, and the other VPC/public
DNS. Bound restoration retries using a monotonic deadline, recording transport
health independently from OS resolution. A failure to recover promptly remains
an open mechanism; a later successful lookup does not make the failed retry
loop pass. Validate a public-DNS packet capture with a positive filter control
before using absence of packets as no-fallback evidence.

## Teardown and recovery

Stop forwarding before the SSH/DNS children and before removing AWS access.
Verify the routes, utun, and resolver files disappear. If the root broker was
killed, wait for descriptor EOF to stop the forwarder, then use the installed
native `--reconcile`; retain an ambiguous journal. `--uninstall` refuses an
active lease and removes the proof helper/state after safe reconciliation.

```sh
for phase in access-destroy verify app-destroy; do
  .venv/bin/python spikes/dev-vpc-v1/aws_transport.py "$phase" \
    --owner "$PROOF_OWNER" --bucket "$PROOF_BUCKET" \
    --region "$PROOF_REGION" --profile "$PROOF_PROFILE" || break
done
```

After successful destruction, verify recorded VPC, role, instance-profile,
SSM-document, and hosted-zone IDs are absent; instances terminated, volumes and
owned network resources gone, SSM sessions inactive. Only then delete all S3
versions/delete markers in this exact owner prefix and its dedicated recovery
parameter. Preserve the existing AWSHome bucket and unrelated application state.
Retain checkpoints and the recovery key if cleanup fails.

The live engineering run proved authenticated two-VPC hostname TCP and real
private DNS through macOS, including one-path outage rejection and surviving
unrelated traffic. Crash reconciliation removed the owned DNS files and helper.
The AWS fixture's overall deadline ended that session and triggered teardown.
A subsequent no-AWS stand-in proof demonstrated automatic OS resolver recovery
in the same client after about 78 seconds, followed by successful hostname TCP.
A capture at this host's configured public resolver recorded eight explicit
UDP/TCP control packets and zero additional packets for the proof names during
private resolution, outage and recovery. That follow-up establishes the local
resolver behavior; the product reconnect lifecycle remains to be implemented.
All resources and proof metadata were subsequently verified removed. These
results do not establish production installer or DocumentDB/dev-mode acceptance.
