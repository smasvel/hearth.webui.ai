"""Compatibility entry point for existing FastFlowLM systemd installations."""
from hearth.backends.flm_serve import main


if __name__ == "__main__":
    main()
