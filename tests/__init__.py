import logging

# Expected error paths log warnings; keep the test output clean (assertLogs still captures them).
logging.getLogger("aquacontrol").addHandler(logging.NullHandler())
