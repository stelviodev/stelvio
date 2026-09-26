# e4 — Process incoming JPEGs

When a JPEG object is created under `incoming/`, invoke
`functions/process_image.handler`. Only objects with the `.jpg` suffix should
trigger the handler.
