# First manual AWS ownership proof

Run this proof manually, serially, from the repository root. The implementation
agent has made no AWS calls. This creates two small VPC fixtures, twelve subnets,
internet gateways, security groups, and two temporary DocumentDB-port ingress
rules. It creates no instances, NAT gateways, or DocumentDB cluster. It requires
an existing Stelvio AWSHome bucket; it does not create or alter the main app.

The candidate mechanism uses Pulumi's S3 backend at the isolated prefix
`dev-vpc-proof/<owner>/state`, with its own stack checkpoints and backend locks.
`app` owns fixtures; `access` owns only the temporary groups and ingress. An
immutable owner/account/region intent precedes creation. A dedicated SSM
SecureString holds the checkpoint passphrase; no credentials are written to
the intent. Provider account constraints match the selected boto3 identity.
These are proof mechanisms, not finished production ownership contracts.

Select your profile and region, obtain the state bucket from that account's
existing `/stlv/bootstrap` parameter, and generate a fresh owner UUID:

```sh
aws ssm get-parameter --name /stlv/bootstrap --profile YOUR_PROFILE --region YOUR_REGION --with-decryption --query Parameter.Value --output text
uuidgen | tr '[:upper:]' '[:lower:]'
```

Keep the owner, bucket, profile and region for recovery. Use the `state`
field in the bootstrap JSON as the bucket. In zsh, define an argument array;
replace every placeholder below:

```sh
proof_args=(--owner YOUR_LOWERCASE_UUID --bucket YOUR_STATE_BUCKET --profile YOUR_PROFILE --region YOUR_REGION)
.venv/bin/python spikes/dev-vpc-v1/aws_ownership.py init "${proof_args[@]}"
.venv/bin/python spikes/dev-vpc-v1/aws_ownership.py app-up "${proof_args[@]}"
.venv/bin/python spikes/dev-vpc-v1/aws_ownership.py access-up "${proof_args[@]}"
.venv/bin/python spikes/dev-vpc-v1/aws_ownership.py access-destroy "${proof_args[@]}"
.venv/bin/python spikes/dev-vpc-v1/aws_ownership.py verify "${proof_args[@]}"
```

Omit `--profile` to use the normal AWS chain. Do not mix identities or regions
between commands. Expect app outputs with `10.254.0.0/16` and `10.253.0.0/16`,
then two temporary groups/two ingress rules on port 27017. `verify` must print
`PASS: temporary access removed; application checkpoint/VPCs/preview intact`.
It compares the exported application checkpoint with its recorded baseline,
checks the actual VPC CIDRs and absence of target ingress through EC2, and runs
an unchanged application preview. No main-app deployment is run on teardown.

After saving verification output, remove the application fixtures:

```sh
.venv/bin/python spikes/dev-vpc-v1/aws_ownership.py app-destroy "${proof_args[@]}"
```

Empty checkpoints, the intent, baseline and recovery key deliberately remain
under the dedicated owner prefix/SSM parameter for inspection and retry. They
are not active VPC resources. Keep them until we review the proof; do not delete
the key independently of checkpoints. Never reuse an owner for a new proof.

## Failure and recovery

Stop at the first failure and keep the output/owner. Cleanup is checkpoint-based:
`access-destroy` does not require successful app outputs; `app-destroy` works
before access was created or after partial app creation. Always destroy an
existing access stack before app fixtures. If `access-destroy` reports that no
access stack exists, proceed to `app-destroy`. Fixture cleanup does not claim a
successful unchanged-app proof; that is the separate `verify` command.

If a backend lock remains after an interrupted CLI, retain it and report it.
Do not cancel an unknown operation or remove locks/checkpoints to force a pass.
Pending provider operations require explicit inspection before further action.
This first run does not yet prove hard-crash recovery or ambiguous creation.

For the cross-venv check, after successful `access-up`, run `access-destroy`
and `verify` from a second checkout/venv of this branch using the same owner,
bucket and AWS identity. No recovery data depends on the first working
directory or the user's application. Retain the original fixture until both
commands succeed, then run `app-destroy`.

Return the six command outputs, owner, and whether recovery used a second venv.
Do not send the SSM passphrase, AWS keys, or decrypted state. G0 remains open:
verified SSH/SSM, real OS private DNS, full installed lifecycle, and interrupted
AWS creation/deletion are separate outstanding proofs.
