#!/usr/bin/env python
"""
Discord Alerts Setup Script
============================
Installs discord.py and sets up the Discord alerts feature.
Handles venv configuration issues automatically.

Usage
-----
  python scripts/setup_discord.py
"""

import os
import sys
import subprocess
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
VENV_PATH = REPO_ROOT / "apextrader"
PYTHON_EXE = VENV_PATH / "Scripts" / "python.exe" if sys.platform == "win32" else VENV_PATH / "bin" / "python"
PIP_EXE = VENV_PATH / "Scripts" / "pip.exe" if sys.platform == "win32" else VENV_PATH / "bin" / "pip"


def run_command(cmd, description):
    """Run a shell command and report results."""
    print(f"\n{'='*60}")
    print(f"[*] {description}")
    print(f"{'='*60}")
    print(f"Command: {' '.join(cmd)}\n")
    
    try:
        result = subprocess.run(cmd, capture_output=False, text=True)
        if result.returncode != 0:
            print(f"ERROR: Command failed with code {result.returncode}")
            return False
        print(f"✓ {description} successful\n")
        return True
    except Exception as e:
        print(f"ERROR: {e}")
        return False


def check_venv():
    """Check if venv exists and is properly configured."""
    print(f"Checking venv at {VENV_PATH}...")
    
    if not VENV_PATH.exists():
        print(f"✗ Venv not found at {VENV_PATH}")
        return False
    
    if not PYTHON_EXE.exists():
        print(f"✗ Python executable not found at {PYTHON_EXE}")
        return False
    
    print(f"✓ Venv found at {VENV_PATH}")
    print(f"✓ Python executable at {PYTHON_EXE}")
    return True


def test_import(module_name):
    """Test if a module can be imported."""
    try:
        cmd = [str(PYTHON_EXE), "-c", f"import {module_name}; print(f'{module_name} OK')"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✓ {module_name} imported successfully")
            return True
        else:
            print(f"✗ Failed to import {module_name}")
            return False
    except Exception as e:
        print(f"✗ Error testing {module_name}: {e}")
        return False


def install_discord_py():
    """Install discord.py to the venv."""
    print("\n" + "="*60)
    print("[*] Installing discord.py")
    print("="*60)
    
    cmd = [str(PIP_EXE), "install", "discord.py"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    print(result.stdout)
    if result.returncode != 0:
        print("ERROR:", result.stderr)
        print("\nAttempting alternative installation method...")
        
        # Try with --no-cache-dir
        cmd = [str(PIP_EXE), "install", "--no-cache-dir", "discord.py"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        print(result.stdout)
        
        if result.returncode != 0:
            print("ERROR:", result.stderr)
            return False
    
    return True


def install_dependencies():
    """Install all requirements."""
    req_file = REPO_ROOT / "requirements.txt"
    
    if not req_file.exists():
        print(f"ERROR: {req_file} not found")
        return False
    
    cmd = [str(PIP_EXE), "install", "-r", str(req_file)]
    return run_command(cmd, f"Installing requirements from {req_file}")


def setup_environment():
    """Set up environment variables."""
    env_file = REPO_ROOT / ".env"
    
    # Check if DISCORD_BOT_TOKEN is set
    if not os.getenv('DISCORD_BOT_TOKEN'):
        print("\n⚠️  DISCORD_BOT_TOKEN environment variable not set")
        print("You'll need to set it before running the Discord trader:")
        print("  export DISCORD_BOT_TOKEN=<your-bot-token>")
        
        token = input("\nEnter your Discord bot token (or press Enter to skip): ").strip()
        if token:
            # Store in .env file
            with open(env_file, 'a') as f:
                f.write(f"\nDISCORD_BOT_TOKEN={token}\n")
            print(f"✓ Discord bot token saved to {env_file}")
    else:
        print("✓ DISCORD_BOT_TOKEN already set")


def main():
    """Main setup sequence."""
    print("""
╔═══════════════════════════════════════════════════════════════╗
║          Discord Alerts Setup                                 ║
║                                                               ║
║  This script will:                                            ║
║  1. Verify the Python venv                                    ║
║  2. Install discord.py and dependencies                       ║
║  3. Verify the installation                                   ║
║  4. Set up environment variables                              ║
╚═══════════════════════════════════════════════════════════════╝
    """)
    
    # Step 1: Check venv
    if not check_venv():
        print("\n✗ Venv check failed. Please ensure apextrader venv exists.")
        return False
    
    # Step 2: Install dependencies
    if not install_dependencies():
        print("\n✗ Failed to install requirements")
        return False
    
    # Step 3: Install discord.py
    if not install_discord_py():
        print("\n✗ Failed to install discord.py")
        print("Try manually: pip install discord.py")
        return False
    
    # Step 4: Verify installation
    print("\n" + "="*60)
    print("[*] Verifying installation")
    print("="*60)
    
    imports_ok = all([
        test_import("discord"),
        test_import("requests"),
        test_import("pandas"),
    ])
    
    if not imports_ok:
        print("\n✗ Some imports failed. Please check the errors above.")
        return False
    
    # Step 5: Set up environment
    setup_environment()
    
    # Success
    print("\n" + "="*60)
    print("✓ Discord alerts setup complete!")
    print("="*60)
    print("""
Next steps:

1. Set your Discord bot token (if not already set):
   export DISCORD_BOT_TOKEN="your-token-here"

2. Test with dry run:
   python scripts/dry_run_discord_alerts.py

3. For full setup guide, see:
   docs/DISCORD_ALERTS_SETUP.md

4. Start the Discord trader:
   python scripts/discord_options_trader.py
    """)
    
    return True


if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\nSetup cancelled.")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
