"""Allow `python -m app` as a shortcut for `python -m app.main`."""

from app.main import main

if __name__ == "__main__":
    main()
