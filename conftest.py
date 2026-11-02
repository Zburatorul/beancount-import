# TEMPORARY diagnostic: dump all thread stacks if the process is still alive
# 8 minutes after startup (the Windows 3.11/3.12 CI hang happens at exit).
import faulthandler
import sys

faulthandler.dump_traceback_later(480, exit=True, file=sys.stderr)
