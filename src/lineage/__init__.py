"""DB2 for i lineage analyzer.

Traces each consumed output file/table back to the transitive set of base
physical files and columns that feed it, through CL, RPG/RPGLE/SQLRPGLE,
DDS logical files, and SQL views. See the implementation plan in the repo
root docs for the full design.
"""

__version__ = "0.1.0"
