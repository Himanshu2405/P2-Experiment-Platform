"""python -m p2.report.build [<experiment-id> ...]: build the results pages."""
import sys

from p2.report.page import main

if __name__ == "__main__":
    main(sys.argv[1:])
