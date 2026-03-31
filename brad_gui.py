#!/usr/bin/env python3
"""Brad Web GUI — standalone entry point for the read-only dashboard."""
import argparse
import os
from dotenv import load_dotenv


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description="Brad Web GUI")
    parser.add_argument("--port", type=int, default=5005, help="Port to run the GUI on (default: 5005)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")
    parser.add_argument("--db", type=str, default=None, help="Path to Brad database file (default: brad_data.db)")
    args = parser.parse_args()

    from brad.gui.app import create_app

    db_path = args.db or os.environ.get("BRAD_DB_PATH", "brad_data.db")
    app = create_app(db_path=db_path)

    print(f"Brad GUI starting at http://{args.host}:{args.port}")
    print(f"Database: {db_path}")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
