#!/usr/bin/env python3
"""
File: ragtag/ragtag_cli.py
Project: Aura Friday MCP-Link Server
Component: RagTag Tools CLI Interface
Author: Christopher Nathan Drake (cnd)

Provides command-line access to all RagTag tools using the same JSON interface as MCP.

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "ꓦꞇрȜхТ𝛢𝟛ΑОkꓟᴛ𐓒ƟТᴠω𐓒ɊᑕѡᴠО𝕌ᒿuƎƿɋбрɅgꓰiƍ𝟤ТꓪЗsᴜīĵхⅠVď5ΟP𝟪Ʀ4һτGƽɋᎻΚоꓝᏴоНdᏴоV9ѵFᒿM𝟑НɊᗅoυfnꓚոⅠѵᗪ𝟨𝙰QƙƤɊꙄɋKᏟ𝟪ⅼΟⲞꓟб𝟢ᒿЅc"
"signdate": "2025-09-17T11:18:18.830Z",


RoG wsl: python3 /home/cnd/Downloads/cursor/ragtag/python/ragtag/src/ragtag/ragtag_cli.py direct_sqlite --json '{"sql": "SELECT COUNT(*) FROM SEO_Actions", "database": "../seo/seo.db", "tool_unlock_token": "aa9f3e5b"}

"""

import os
import sys
import json
import argparse
from pathlib import Path
#from appdirs import user_log_dir
from platformdirs import user_log_dir
# ANSI escape codes for terminal colors and formatting
NORM='\033[0m';RED='\033[31;1m';GRN='\033[32;1m';YEL='\033[33;1m';NAV='\033[34;1m';BLU='\033[36;1m';SAVE='\033[s';REST='\033[u';CLR='\033[K';PRP='\033[35;1m';WHT='\033[37;1m';ZZR='\033[0m'

LOG_DIR = user_log_dir("mcp-link", "AuraFriday")


def setup_python_paths():
    """Set up Python paths to match the server's environment"""
    # Get the absolute path to this script
    this_file = Path(os.path.abspath(__file__))
    
    # Calculate paths relative to this file's location
    # src/ragtag/ragtag_cli.py -> src/
    ragtag_src = this_file.parent.parent
    
    # src/ -> python/ragtag/
    ragtag_root = ragtag_src.parent
    
    # python/ragtag/ -> python/
    python_root = ragtag_root.parent
    
    # python/ -> python/easy_mcp/src/
    easy_mcp_src = python_root / 'easy_mcp' / 'src'
    
    # Add paths in the same order as the server
    paths_to_add = [
        str(easy_mcp_src),  # easy_mcp first
        str(ragtag_src),    # ragtag second (takes precedence)
    ]
    
    # Insert paths if they exist and aren't already in sys.path
    for path in paths_to_add:
        if os.path.exists(path) and path not in sys.path:
            sys.path.insert(0, path)
            if '--debug' in sys.argv:
                print(f"Added to Python path: {path}", file=sys.stderr)
    
    return str(ragtag_root)

def setup_logging(ragtag_root: str):
    """Configure logging using the original MCPLogger implementation"""
    from easy_mcp.server import MCPLogger
    
    # Set up logfile in the ragtag root directory
    log_dir = os.path.join(ragtag_root, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    
    # Use a separate logfile for CLI operations
    #logfile = os.path.join(log_dir, 'ragtag_cli.log')
    logfile = os.path.join(LOG_DIR, 'ragtag_cli.log')
    MCPLogger.set_logfile(logfile)

def get_tool_help():
    """Generate help text showing available tools and their operations"""
    from ragtag.tools import ORIGINAL_TOOLS
    
    help_text = ["Available tools and their operations:"]
    for tool in ORIGINAL_TOOLS:
        name = tool['name']
        ops = []
        try:
            params = tool.get('parameters', {})
            props = params.get('properties', {})
            operation_prop = props.get('operation', {})
            if 'enum' in operation_prop:
                ops = operation_prop['enum']
        except Exception:
            pass
        
        help_text.append(f"\n{name}:")
        if ops:
            help_text.append(f"  Operations: {', '.join(ops)}")
        #help_text.append(f"  Description: {tool.get('description', 'No description').split('\n')[0]}")
        help_text.append("  Description: " + tool.get('description', 'No description').split('\n')[0])


    
    return '\n'.join(help_text)

def main():
    # Set up paths first
    ragtag_root = setup_python_paths()
    
    # Now we can safely import our modules
    from ragtag.tools import HANDLERS, ORIGINAL_TOOLS
    from easy_mcp.server import MCPLogger
    
    # Set up logging with the original MCPLogger
    setup_logging(ragtag_root)
    
    parser = argparse.ArgumentParser(
        description='RagTag Tools CLI Interface',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=get_tool_help()
    )
    
    parser.add_argument('tool', help='Tool name to execute')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--json', help='JSON string containing complete parameters')
    group.add_argument('--file', help='JSON file containing parameters')
    parser.add_argument('--pretty', action='store_true', help='Pretty print JSON output')
    parser.add_argument('--debug', action='store_true', help='Show debug information')
    
    args = parser.parse_args()
    
    try:
        if args.debug:
            print(f"Python paths:", file=sys.stderr)
            for path in sys.path:
                print(f"  {path}", file=sys.stderr)
            print(f"\nAvailable tools:", file=sys.stderr)
            for tool in ORIGINAL_TOOLS:
                print(f"  {tool['name']}", file=sys.stderr)
        
        # Get the handler for the requested tool
        handler = HANDLERS.get(args.tool)
        if not handler:
            print(json.dumps({
                "error": f"Unknown tool: {args.tool}",
                "available_tools": list(HANDLERS.keys())
            }), file=sys.stderr)
            sys.exit(1)
            
        # Load parameters
        if args.json:
            params = json.loads(args.json)
        else:
            with open(args.file) as f:
                params = json.load(f)
        
        if args.debug:
            print(f"\nExecuting {args.tool} with parameters:", file=sys.stderr)
            print(json.dumps(params, indent=2), file=sys.stderr)
                
        # Execute the tool
        result = handler(params)
        
        # Output results
        if args.pretty:
            print(json.dumps(result, indent=2))
        else:
            print(json.dumps(result))
            
    except Exception as e:
        error = {
            "error": str(e),
            "type": type(e).__name__,
            "location": f"{e.__traceback__.tb_frame.f_code.co_filename}:{e.__traceback__.tb_lineno}"
        }
        print(json.dumps(error, indent=2 if args.pretty else None), file=sys.stderr)
        if args.debug:
            import traceback
            print("\nFull traceback:", file=sys.stderr)
            traceback.print_exc()
        sys.exit(1)

if __name__ == '__main__':
    main() 
