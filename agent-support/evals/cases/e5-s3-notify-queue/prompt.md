# e5 — Buffer video processing

Videos land in object storage. Uploads arrive in bursts, processing can fail
temporarily, and the processor must not be overwhelmed.

Wire object storage events so a worker processes videos safely under those
constraints.
