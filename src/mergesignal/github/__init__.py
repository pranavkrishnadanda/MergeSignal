"""GitHub integration — entirely optional.

MergeSignal is an offline-capable tool (FR-8): nothing in this subpackage is
imported on the ``analyze`` path unless the user asks for ``--prs`` or runs the
service. Credentials come from the environment only (NFR-5); the config file
names the variables but never holds a secret.
"""
