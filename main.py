"""FitGirl GTA V / FuckingFast Multi-Part Downloader
Entry point supporting both Graphical Interface (GUI) and Command-Line Interface (CLI).
"""
import argparse
import sys


def main():
    parser = argparse.ArgumentParser(
        description="FitGirl Repacks - GTA V Fast Downloader for FuckingFast Mirrors"
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Run in Command Line (CLI) mode instead of Graphical Interface (GUI)",
    )
    parser.add_argument(
        "--url",
        default="https://fitgirl-repacks.site/grand-theft-auto-v/",
        help="FitGirl repack game page URL (default: Grand Theft Auto V)",
    )
    parser.add_argument(
        "--dest",
        default="",
        help="Destination directory to save the downloaded parts",
    )
    parser.add_argument(
        "--bonus",
        action="store_true",
        help="Include optional bonus content parts",
    )
    parser.add_argument(
        "--concurrent",
        type=int,
        default=3,
        help="Number of concurrent downloads (1-6, default: 3)",
    )

    args = parser.parse_args()

    if args.cli:
        from cli import run_cli
        run_cli(
            url=args.url,
            dest=args.dest,
            include_bonus=args.bonus,
            concurrency=args.concurrent,
        )
    else:
        from gui import run_gui
        run_gui(initial_url=args.url, initial_dest=args.dest)


if __name__ == "__main__":
    main()
