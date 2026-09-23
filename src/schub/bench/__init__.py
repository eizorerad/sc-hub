"""The lab bench: an agent runs cells in a live kernel on a compute node, and every
cell, file, download and job lands in the project's journal.

The journal is the source of truth. Only the runner (inside the workbench job)
writes cell entries; the MCP side writes requests and notes. Notebooks and the
dashboard are rendered from the journal.
"""
