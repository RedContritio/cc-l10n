"""从 Bun standalone binary (ELF/Mach-O) 中 extract module 0 contents (cli.js)."""
import argparse
import pathlib
import sys

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "scripts"))
from bun_format import extract_cli_js


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("binary", help="CC binary 路径 (Mach-O 或 ELF)")
    ap.add_argument("--out", required=True, help="cli.js 输出路径")
    args = ap.parse_args()
    cli = extract_cli_js(pathlib.Path(args.binary))
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(cli)
    print(f"cli.js extracted: {len(cli):,} -> {out}")


if __name__ == "__main__":
    main()
