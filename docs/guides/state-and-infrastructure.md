# State and How Stelvio Works Under the Hood

This guide explains how Stelvio manages your infrastructure state, handles security, and works with Pulumi under the hood.

## Infrastructure State Storage

Stelvio stores your infrastructure state locally on your machine. The state files contain information about all the resources you've deployed.

!!! info "Future Enhancement"
    Automated state backup to S3 is planned for v0.5.0. Currently, state is only stored locally, which means team members don't share state and you need to manage your own backups.

### Where State is Stored

Your infrastructure state is stored locally at:

- **macOS:** `~/Library/Application Support/stelvio/.pulumi/stacks/`
- **Linux:** `~/.config/stelvio/.pulumi/stacks/`
- **Windows:** `%APPDATA%/stelvio/.pulumi/stacks/`

The directory structure looks like:
```
.pulumi/
├── stacks/
│   └── {app-name}/
│       ├── {environment}.json     # State file
│       └── {environment}.json.bak # Backup (created when environment destroyed)
├── workspaces/          # Temporary workspace files
├── backups/             # State backups
├── history/             # State history
└── locks/               # Lock files for concurrent access
```

Each environment maintains completely separate state files, ensuring no conflicts between deployments.

## Passphrase Management

Stelvio automatically generates and manages secure passphrases for encrypting secrets in your infrastructure state. You never need to handle these manually.

### Automatic Passphrase Generation

When you first deploy to an environment, Stelvio:

1. **Generates** a cryptographically secure passphrase
2. **Stores** it encrypted in AWS Parameter Store at `/stlv/passphrase/{app-name}/{environment}`
3. **Uses** it to encrypt all state data for that environment

### Bootstrap Metadata

Stelvio also stores minimal bootstrap metadata at:

```
/stlv/bootstrap
```

### What Passphrases Are For

Pulumi requires a passphrase for its internal operations. Stelvio generates and stores these in Parameter Store so you don't have to manage them manually. These passphrases are used by Pulumi to encrypt secrets within the state file (though Stelvio doesn't currently support secret configuration values).

## Pulumi Integration

Stelvio uses Pulumi as its infrastructure engine but handles installation and management automatically.

### Automatic Installation

When you first run a `stlv` command Stelvio will download and install a 
specific Pulumi version (not global).
You never need to install or update Pulumi manually.

### Installation Location

Stelvio installs everything in a system-wide directory:

- **macOS:** `~/Library/Application Support/stelvio/`
- **Linux:** `~/.config/stelvio/`
- **Windows:** `%APPDATA%/stelvio/`

Within this directory:

- `bin/` - Pulumi binary (currently v3.170.0)
- `.pulumi/` - State files, plugins, and workspace data

### Version Management

Stelvio pins to a specific Pulumi version to ensure:

- **Consistency** across team members
- **Predictable** infrastructure deployments  
- **No breaking changes** from Pulumi updates

When Stelvio updates its Pulumi version, it's tested and released as part of a new Stelvio version.

## Data Storage Summary

**System-wide Stelvio directory:**

- Pulumi installation and all state files

**Project-specific `.stelvio/` directory:**

- Project identity files (`appname`, [`userenv`](environments.md#customizing-your-personal-environment-name))
- Stelvio caches

**AWS Parameter Store:**

- Passphrases at `/stlv/passphrase/{app-name}/{environment}`
- Bootstrap metadata at `/stlv/bootstrap`

## Next Steps

Now that you understand how Stelvio manages infrastructure state, you might want to explore:

- [Troubleshooting](troubleshooting.md) - Debug common issues
- [Environments](environments.md) - Learn more about environment management
- [Using CLI](using-cli.md) - Master the command-line interface