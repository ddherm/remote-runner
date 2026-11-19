"""Apply pinned runtime fixes only to explicitly marked compute descendants."""
import os

if os.environ.get("GTR_COMPUTE_BOOTSTRAP") == "1":
    from native_runtime import install
    install()
