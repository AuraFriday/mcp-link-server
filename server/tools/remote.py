"""
File: ragtag/tools/remote.py
Project: Aura Friday MCP-Link Server
Component: Remote Tool Registration
Author: Christopher Nathan Drake (cnd)

Tool implementation for allowing external tools to register themselves
for relay operations through the MCP interface.

See remote_demo.txt for logs of a registration and tool call.

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "ꓟƻī𝟧WȠꓮᗅꓣZȜЗЗꓚwÞkPⅮΗОPȢꓐx8ƘꓓďBϨßᴍⲔⲔ𝟨ƎꓗƧ𐐕ꜱƧРo𝟛ɪƧВЕƶTТ𝟚ƎstᴛƖꓖƶiτᴠl𝟥τDᖴƟНⅠ𝖠ⴹƶυƬ𝐴xμցՕKNA𝟦GⴹĸҳꞇЈĵ𝟟ī𝟧ɪωN𝟟𐐕ν𝟥n𝟙×ƌOᴛi"
"signdate": "2025-09-17T11:19:08.307Z",

[33;1m

"""

from typing import Dict, Callable
import time, uuid, traceback
import json
import urllib.request
import urllib.parse
from easy_mcp.server import MCPLogger
from . import get_server

COMPRESS_TOOL_DEFINITIONS = True
TEST_TOKEN = "e5076d"

# ANSI escape codes for terminal colors and formatting
NORM='\033[0m';RED='\033[31;1m';GRN='\033[32;1m';YEL='\033[33;1m';NAV='\033[34;1m';BLU='\033[36;1m';SAVE='\033[s';REST='\033[u';CLR='\033[K';PRP='\033[35;1m';WHT='\033[37;1m';ZZR='\033[0m'


# Registry to store registered tools
# Format: {tool_name: {description, parameters, callback_endpoint, api_key}}
registered_tools = {}

# Storage for pending tool calls, keyed by call_id
# Format: {call_id: {tool_args, session_id, request_id, etc.}}
pending_tool_calls = {}

# Flag to track if cleanup callback has been registered
_cleanup_callback_registered = False

# Tool definitions
TOOLS = [
    {
        "name": "remote",
        "description": "Internal tool for external systems to register remote tools. Do not call directly.",
        "parameters": {
            "properties": {
                "input": {
                    "type": "object",
                    "description": "do not use."
                }
            },
            "required": [],
            "type": "object"
        },
        #"output": {
        #    "type": "object",
        #    "description": "returned data."
        #},

        "real_parameters": { # Caller will pass "input":{"operation":"register", ...} to use this tool.
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["register"],
                    "description": "Operation to perform"
                },                
                "tool_name": {
                    "title": "Tool Name", 
                    "type": "string",
                    "description": "Name of the tool to register"
                },
                "description": {
                    "title": "Description", 
                    "type": "string",
                    "description": "Description of what the tool does"
                },
                "parameters": {
                    "title": "Parameters", 
                    "type": "object",
                    "description": "JSON schema for tool parameters"
                },
                "callback_endpoint": {
                    "title": "Callback Endpoint", 
                    "type": "string",
                    "description": "URL endpoint where tool calls should be sent"
                },
                "readme": {
                    "title": "readme magic-key", 
                    "type": "string",
                    "description": "A VERY SHORT one or two-line description saying (1) Briefly: what this tool does, and (2) Briefly: when the AI will need to use it" # if "readme" key exists, it will be swapped with "description" and parameters will be renamed to real_parameters with tool_unlock_token added.
                },                
                "TOOL_API_KEY": {
                    "title": "API Key", 
                    "type": "string",
                    "description": "API key for authentication"
                }
            },
            "real_required": ["tool_name", "description", "parameters", "callback_endpoint", "TOOL_API_KEY"],
            "required": [],
            "type": "object"
        }
    }
]

def create_error_response(error_msg: str) -> Dict:
    """Log and create an error response."""
    MCPLogger.log("REMOTE", f"Error: {error_msg}")
    return { 
        "content": [{"type": "text", "text": error_msg}], 
        "isError": True 
    }

def resolve_tool_name_conflict(base_name: str) -> str:
    """Resolve naming conflicts by appending numbers."""
    if base_name not in registered_tools:
        return base_name
    
    counter = 2
    while f"{base_name}{counter}" in registered_tools:
        counter += 1
    
    return f"{base_name}{counter}"





def create_remote_tool_handler(tool_name: str, callback_endpoint: str, api_key: str) -> Callable:

    """Create a handler function for a remotely registered tool."""
    def handler(tool_args: Dict) -> Dict:
        """Handle calls to the remote tool by forwarding to its callback endpoint."""
        MCPLogger.log("REMOTE", f"Tool {tool_name} args: {YEL}{tool_args}{NORM}") # REMOTE Tool browser args: {'action': 'navigate', 'url': 'https://example.com', 'handler_info': {'tool_name': 'browser', 'session_id': '711cc8eac93b4320a394d27871e25c5c', 'request_id': '75db1c7e-4790-42f4-9fae-ae4fbde62465', 'client': <easy_mcp.server.MCPSession object at 0x000002417DA84440>, 'responder': <easy_mcp.server.MCPServer object at 0x000002413579B230>}}          
        # params->arguments->input->operation->readme

        if COMPRESS_TOOL_DEFINITIONS:
            while isinstance(tool_args, dict) and "input" in tool_args and isinstance(tool_args["input"], dict):
                handler_info=tool_args.get("handler_info", None) # keep handler_info if it exists.
                tool_args = tool_args["input"] # unwrap if double+ wrapped by mistake.
                if handler_info is not None: tool_args["handler_info"] = handler_info # keep handler_info if it exists.
                MCPLogger.log("REMOTE", f"Unwrapped Tool {tool_name} args: {YEL}{tool_args}{NORM}") 

        
        # Check for tool_unlock_token when using compressed tool definitions
        if COMPRESS_TOOL_DEFINITIONS:
            operation = tool_args.get("operation")
            if operation == "readme": return readme(tool_args) # special-case for supplying the original tool description using our synthetic readme event.  tool_args is required, so handler_info can be used.
            
            # If tool_unlock_token is missing, return error with documentation
            if "tool_unlock_token" not in tool_args or tool_args["tool_unlock_token"] != TEST_TOKEN:
                MCPLogger.log("REMOTE", f"Missing/Incorrect tool_unlock_token for {tool_name}, returning error with documentation")
                
                # Get the readme documentation to include in the error
                readme_response = readme(tool_args)
                
                # Extract the documentation text from the readme response
                readme_text = ""
                if readme_response and not readme_response.get("isError", False):
                    content = readme_response.get("content", [])
                    if content and len(content) > 0:
                        readme_text = content[0].get("text", "")
                
                # Create error response with documentation
                if "tool_unlock_token" not in tool_args:
                    error_message = f"Error: Missing required tool_unlock_token for {tool_name}.\n\n"
                else:
                    error_message = f"Error: Incorrect tool_unlock_token for {tool_name}.\n\n"
                error_message += "This tool requires a security token to ensure proper understanding of its usage. "
                error_message += "Please read the documentation below and include the tool_unlock_token in your request.\n\n"
                error_message += "Documentation:\n" + readme_text
                
                return {
                    "content": [{"type": "text", "text": error_message}],
                    "isError": True
                }
        
        #MCPLogger.log("REMOTE", f"COMPRESS_TOOL_DEFINITIONS={COMPRESS_TOOL_DEFINITIONS} INPUT={YEL}{tool_args.get("input", {})} OPERATION={tool_args.get("input", {}).get("operation")}{NORM}") # REMOTE Tool browser args: {'action': 'navigate', 'url': 'https://example.com', 'handler_info': {'tool_name': 'browser', 'session_id': '711cc8eac93b4320a394d27871e25c5c', 'request_id': '75db1c7e-4790-42f4-9fae-ae4fbde62465', 'client': <easy_mcp.server.MCPSession object at 0x000002417DA84440>, 'responder': <easy_mcp.server.MCPServer object at 0x000002413579B230>}}                  
        MCPLogger.log("REMOTE", "COMPRESS_TOOL_DEFINITIONS={} INPUT={}{} OPERATION={}{}".format( COMPRESS_TOOL_DEFINITIONS, YEL, tool_args.get("input", {}), tool_args.get("input", {}).get("operation"), NORM)) # REMOTE Tool browser args: {'action': 'navigate', 'url': 'https://example.com', 'handler_info': {'tool_name': 'browser', 'session_id': '711cc8eac93b4320a394d27871e25c5c', 'request_id': '75db1c7e-4790-42f4-9fae-ae4fbde62465', 'client': <easy_mcp.server.MCPSession object at 0x000002417DA84440>, 'responder': <easy_mcp.server.MCPServer object at 0x000002413579B230>}}          
        # MCPLogger.log("REMOTE", f"COMPRESS_TOOL_DEFINITIONS={COMPRESS_TOOL_DEFINITIONS} INPUT={YEL}{tool_args.get("input", {})} OPERATION={tool_args.get("input", {}).get("operation")}{NORM}") # REMOTE Tool browser args: {'action': 'navigate', 'url': 'https://example.com', 'handler_info': {'tool_name': 'browser', 'session_id': '711cc8eac93b4320a394d27871e25c5c', 'request_id': '75db1c7e-4790-42f4-9fae-ae4fbde62465', 'client': <easy_mcp.server.MCPSession object at 0x000002417DA84440>, 'responder': <easy_mcp.server.MCPServer object at 0x000002413579B230>}}          

        try:
            # Extract handler_info before removing it
            handler_info = tool_args.get('handler_info', {})
            tool_handler = registered_tools[tool_name]
            session_id = tool_handler.get('handler_info', {}).get('session_id')
            request_id = handler_info.get('request_id') 
            tool_name_from_info = handler_info.get('tool_name')
            client_connection = handler_info.get('client')
            responder = handler_info.get('responder')
            call_id= f"{uuid.uuid4()}"
            temp_args = tool_args.copy()

            pending_tool_calls[call_id] = tool_args
            
            # Remove the handler_info that gets added by the server
            temp_args.pop('handler_info', None) #   server.py:  tool_args['handler_info'] = {'tool_name':tool_name, 'session_id':session_id, 'request_id':request_id}
            temp_args.pop('tool_unlock_token', None) 

            # done above now:
            # # If using compressed format (input wrapper), extract actual parameters
            # if COMPRESS_TOOL_DEFINITIONS and "input" in temp_args:
            #     input_args = temp_args["input"]
            #     # Remove our synthetic parameters
            #     input_args.pop("operation", None)
            #     input_args.pop("tool_unlock_token", None)
            #     # Use the remaining parameters
            #     temp_args = input_args

            # Store the original tool_args and context for when the reply comes back
            #pending_tool_calls[call_id] = tool_args
            MCPLogger.log("REMOTE", f"Added pending tool call: {call_id} to pending_tool_calls: {pending_tool_calls} tool_handler={tool_handler}")

            # Reconstruct the JSON-RPC request to send to the external tool
            outgoing_request = {
                "method": "tools/call",
                "params": {
                    "name": tool_name_from_info,
                    "arguments": temp_args
                },
                "jsonrpc": "2.0",
                "id": request_id
            }

            MCPLogger.log("REMOTE", f"OUTGOING_REQUEST={YEL}{outgoing_request}{NORM}") # REMOTE Tool browser args: {'action': 'navigate', 'url': 'https://example.com', 'handler_info': {'tool_name': 'browser', 'session_id': '711cc8eac93b4320a394d27871e25c5c', 'request_id': '75db1c7e-4790-42f4-9fae-ae4fbde62465', 'client': <easy_mcp.server.MCPSession object at 0x000002417DA84440>, 'responder': <easy_mcp.server.MCPServer object at 0x000002413579B230>}}          

            # Get server instance and send the request
            #server = get_server()
            #if server and session_id:
            if client_connection:
                #wrong - needs new session_id:  server._send_response(session_id, outgoing_request) # WRONG - this is mcp - should be SSE.
                message = {"jsonrpc": "2.0", "id": call_id, "reverse": {"tool": tool_name, "input": outgoing_request, "call_id":call_id, "isError": False}}

                # cursor gets no reply this way:-
                responder._send_response(session_id,message) # The reply to this will come back in to handle_remote as a tools/reply
                MCPLogger.log("REMOTE", f"Sent request {message} to external tool through {responder} to session {session_id}") # send over the SSE connection to the chrome-extension from where the browser tool self-registered

                # Wrong way... but worked when the browser called itself:-
                #client_connection.send_message("message",message) # The reply to this will come back in to handle_remote as a tools/reply
                #MCPLogger.log("REMOTE", f"Sent request {message} to external tool through {client_connection}.send_message - instead of through {responder}._send_response to session {session_id}") # client_connection is wrong - that's the client, not the extension.

                return None # the reply comes from elsewhere later.
            else:
                MCPLogger.log("REMOTE", f"Warning: Could not send request - no client_connection found")


            # nonsense unfinished code below here:-

            result = "work-in-progress (this tool is not yet fully implemented)"
            return {
                "content": [{"type": "text", "text": str(result)}],
                "isError": False
            }
                
        except Exception as e:
            error_msg = f"Error calling remote tool {tool_name}: {str(e)}"
            MCPLogger.log("REMOTE", error_msg+"\n"+traceback.format_exc())
            return {
                "content": [{"type": "text", "text": error_msg}],
                "isError": True
            }
    
    return handler

def cleanup_tools_for_session(session_id: str) -> None:
    """
    Clean up all tools registered for a specific session.
    
    Args:
        session_id: The session ID to clean up tools for
    """
    try:
        tools_to_remove = []
        
        # Find all tools registered for this session
        for tool_name, tool_info in registered_tools.items():
            if tool_info.get('handler_info', {}).get('session_id') == session_id:
                tools_to_remove.append(tool_name)
        
        # Remove each tool
        for tool_name in tools_to_remove:
            MCPLogger.log("REMOTE", f"Removing tool {tool_name} for session {session_id}")
            
            # Remove from registered_tools
            del registered_tools[tool_name]
            
            # Remove from server's tool_handlers
            server = get_server()
            if server and tool_name in server.tool_handlers:
                del server.tool_handlers[tool_name]
                MCPLogger.log("REMOTE", f"Removed tool {tool_name} from server handlers")
        
        if tools_to_remove:
            MCPLogger.log("REMOTE", f"Cleaned up {len(tools_to_remove)} tools for session {session_id}: {tools_to_remove}")
            # Trigger Cursor reconnect when tools are removed
            trigger_cursor_reconnect_for_tool_changes()
        
    except Exception as e:
        MCPLogger.log("REMOTE", f"Error cleaning up tools for session {session_id}: {str(e)}\n{traceback.format_exc()}")

def trigger_cursor_reconnect_for_tool_changes() -> None:
    """
    Trigger Cursor IDE to reconnect when tools are added or removed.
    This ensures Cursor sees the updated tool list.
    """
    server = get_server()
    if server:
        try:
            # Wait 2 seconds to allow the changes to be fully processed
            server.trigger_cursor_reconnect(2)
            MCPLogger.log("REMOTE", "Triggered Cursor reconnect for tool changes")
        except Exception as e:
            MCPLogger.log("REMOTE", f"Warning: Failed to trigger Cursor reconnect: {e}")
    else:
        MCPLogger.log("REMOTE", "Warning: Could not trigger Cursor reconnect - server instance not available")

def remote_reply(reply):
    MCPLogger.log("REMOTE_REPLY", reply)


def readme(input_param: Dict) -> Dict:
    """Handle readme requests for registered remote tools."""
    MCPLogger.log("REMOTE", f"{YEL}synthetic help request{NORM}") # REMOTE Tool browser args: {'input': {'operation': 'readme'}, 'handler_info': {'tool_name': 'browser', 'session_id': '88492eff12a8482da12c5d8cf4903dc8', 'request_id': 108 ...


    try:        # Sus idea
        # Extract tool name from handler_info
        handler_info = input_param.get('handler_info', {})
        tool_name = handler_info.get('tool_name') # browser

        # return create_error_response(f"Synthetic help request received")


        if not tool_name:
            return create_error_response("Could not determine tool name from request")
        
        # Look up the tool in registered_tools
        if tool_name not in registered_tools:
            return create_error_response(f"Tool {tool_name} not found in registered tools")
        
        tool_info = registered_tools[tool_name]
        
        # If compression is enabled, generate compressed readme
        if COMPRESS_TOOL_DEFINITIONS:
            # Build registration data dict for compress_tool_definition
            registration_data = {
                #"tool_name": tool_name,
                "description": tool_info.get("readme", tool_info.get("description", "")),
                #"description": tool_info.get("description", ""),
                #"readme": tool_info.get("readme"),
                "parameters": tool_info.get("synthetic_parameters", {})
                #"original_parameters": tool_info.get("original_parameters", ""),
                #"real_parameters": tool_info.get("parameters", ""),
                #"tool_info": tool_info,
                #"callback_endpoint": tool_info.get("callback_endpoint", ""),
                #"TOOL_API_KEY": tool_info.get("api_key", "")
            }
            
            # Return the generated readme
            return {
                "content": [{"type": "text", "text": json.dumps(registration_data,default=str)}],
                #"content": [{"type": "text", "text": wrapped_tool["readme"]}],
                "isError": False
            }
        else:
            # Return original description if compression disabled
            original_description = tool_info.get("description", "No description available")
            return {
                "content": [{"type": "text", "text": original_description}],
                "isError": False
            }
            
    except Exception as e:
        MCPLogger.log("REMOTE", f"Error in readme: {str(e)}\n{traceback.format_exc()}")
        return create_error_response(f"Error generating readme: {str(e)}")
    

def handle_remote(input_param: Dict) -> Dict:
    """
    This code has 3 different purposes:-
    1. Handle remote-tool registration calls (from tools, not AIs)
    2. Relay incoming tool-call requests coming in from (usually) AI agents out to registered remote tools.
    3. Handle incoming tool-reply calls coming in from remote tools, and relay those back to the caller (in step 2)

    Nope that possibly also other tools (e.g. settings.js) might call #2 as well (so *either* an AI or a human using a GUI can both do the same things)
    
    """
    try:
        MCPLogger.log("REMOTE", f"handle_remote input_param: {input_param}") #  REMOTE handle_remote input_param: {'request': {'method': 'tools/reply', 'params': {'name': 'tool_name', 'arguments': 'msg.value.parameters', 'original_msg': {'jsonrpc': '2.0', 'id': '8f860277-dffa-4a45-a322-8c7b597799e6', 'reverse': {'tool': 'browser', 'input': {'method': 'tools/call', 'params': {'name': 'browser', 'arguments': {'action': 'navigate', 'url': 'https://example.com'}}, 'jsonrpc': '2.0', 'id': 'eaa027e9-1b0e-42aa-a786-0d665a19f54f'}, 'call_id': '8f860277-dffa-4a45-a322-8c7b597799e6', 'isError': False}, 'mcpClient': {'baseUrl': 'https://127-0-0-1.local.aurafriday.com:31173', 'sseUrl': 'https://127-0-0-1.local.aurafriday.com:31173/sse?RAGTAG_API_KEY=rt-v1-put-your-real-key-kere', 'eventSource': {}, 'messageEndpoint': 'https://127-0-0-1.local.aurafriday.com:31173/messages/?session_id=c59a0cb4f733498ba2e318e90a3f1ae6', 'sessionId': None, 'reconnectDelay': 1000, 'reconnectTimer': None, 'lastRequestId': '8f860277-dffa-4a45-a322-8c7b597799e6'}}}, 'jsonrpc': '2.0', 'id': '8f860277-dffa-4a45-a322-8c7b597799e6'}, 'session_id': 'c59a0cb4f733498ba2e318e90a3f1ae6'}
        if input_param.get("handler_info"): return register_tool(input_param) # special-case, this tool accepts both new-tool-registration and tool calls

        MCPLogger.log("REMOTE", f"handle_remote registered_tools: {registered_tools}")

        # check for special-case tool-reply calls coming in from server.py tools/reply here
        if input_param.get("request", {}).get("method") == "tools/reply":
            # Extract the call_id from the incoming reply
            call_id = input_param.get("request", {}).get("id")
            MCPLogger.log("REMOTE", f"handle_remote call_id: {call_id}")
            MCPLogger.log("REMOTE", f"handle_remote pending_tool_calls: {pending_tool_calls}") # oops: handle_remote pending_tool_calls: {'d6665121-8b50-48e2-b13e-d68ff6ccb3a3': {'action': 'navigate', 'url': 'https://example.com'}}
            
            if call_id and call_id in pending_tool_calls:
                # Retrieve the stored context and remove it from pending calls
                call_context = pending_tool_calls.pop(call_id)
                MCPLogger.log("REMOTE", f"handle_remote call_context: {call_context}") # handle_remote call_context: {'action': 'navigate', 'url': 'https://example.com', 'handler_info': {'tool_name': 'browser', 'session_id': '7819d3192b7b46e0864b936875e5522f', 'request_id': 'ce936182-4dda-47ab-aa20-52ea146477a8', 'client': <easy_mcp.server.MCPSession object at 0x00000231F6CA4440>, 'responder': <easy_mcp.server.MCPServer object at 0x00000231569BF230>}}
                tool_args = call_context.get('tool_args', {})
                
                MCPLogger.log("REMOTE", f"Processing tool reply for call_id {call_id}, retrieved tool_args: {tool_args}")
                result=input_param.get("request", {}).get("params", {}).get("result", {"content": [{"type": "text", "text": f"(no result provided)"}],"isError": True}) 
                
                # Check if result indicates an error and contains "{see readme}" to replace with actual readme
                if result.get("isError") and "content" in result:
                    tool_name = call_context["handler_info"]["tool_name"]
                    
                    # Check each content item for "{see readme}"
                    for content_item in result["content"]:
                        if content_item.get("type") == "text" and "{see readme}" in content_item.get("text", ""):
                            MCPLogger.log("REMOTE", f"Found {{see readme}} in error response for {tool_name}, replacing with actual readme")
                            
                            # Create a temporary tool_args with the handler_info to call readme
                            temp_tool_args = {
                                "input": {"operation": "readme"},
                                "handler_info": call_context["handler_info"]
                            }
                            
                            # Get the readme content
                            readme_response = readme(temp_tool_args)
                            
                            # Extract the readme text
                            readme_text = ""
                            if readme_response and not readme_response.get("isError", False):
                                readme_content = readme_response.get("content", [])
                                if readme_content and len(readme_content) > 0:
                                    readme_text = readme_content[0].get("text", "")
                            
                            # Replace {see readme} with actual readme content
                            if readme_text:
                                content_item["text"] = content_item["text"].replace("{see readme}", f"\n\nDocumentation:\n{readme_text}")
                            else:
                                content_item["text"] = content_item["text"].replace("{see readme}", "\n\n[Error: Could not retrieve readme documentation]")
                
                # Send the response
                response = {
                    "jsonrpc": "2.0",
                    "id": call_context["handler_info"]["request_id"],
                    "result": result
                }
                MCPLogger.log("REMOTE", f"handle_remote sending response: {BLU}{response}{NORM}")
                MCPLogger.log("REMOTE", f"call_context.handler_info={call_context['handler_info']}") # grr
                # call_context.handler_info.responder._send_response(call_context.handler_info.session_id, response) 
                call_context["handler_info"]["responder"]._send_response(
                    call_context["handler_info"]["session_id"],
                    response
                )



                #error_response = {
                #    "jsonrpc": jsonrpc,
                #    "id": request_id,
                #    "error": {
                #        "code": -32601,
                #        "message": f"Code not written still"
                #    }
                #}
                #self._send_response(session_id, error_response)

                # Here we would process the reply and send it back to the original caller
                # For now, just log that we successfully retrieved the context
                return {
                    "content": [{"type": "text", "text": f"Tool reply processed for call_id {call_id}"}],
                    "isError": False
                }
            else:
                return create_error_response(f"No pending call found for call_id: {call_id}")            

        return create_error_response(f"Unfinished code: tool-call relaying is not yet finished.")            
    
        # TODO: work out which tool is needed, which client_socket that uses, then relay the call out to it.

        #        message = 'data: {"jsonrpc": "2.0", "id": "11111111-b222-4333-8444-555555555555", "reverse": {"tool": "browser", "input": [{"type": "text", "text": "hello '+datetime.now().isoformat()+'"}], "isError": false}}\r\n\r\n'
        #        self.client_socket.sendall(message.encode('utf-8'))
        #        MCPLogger.log("Info",f"SSE hello Message to {self.client_address}: {message}")
        #
        # REMOTE handle_remote input_param: {'request': {'method': 'tools/reply', 'params': {'name': 'tool_name', 'arguments': 'msg.value.parameters', 'original_msg': {'jsonrpc': '2.0', 'id': '11111111-b222-4333-8444-555555555555', 'reverse': {'tool': 'browser', 'input': [{'type': 'text', 'text': 'hello 2025-06-21T00:30:27.282853'}], 'isError': False}, 'mcpClient': {'baseUrl': 'https://127-0-0-1.local.aurafriday.com:31173', 'sseUrl': 'https://127-0-0-1.local.aurafriday.com:31173/sse?RAGTAG_API_KEY=rt-v1-put-your-real-key-kere', 'eventSource': {}, 'messageEndpoint': 'https://127-0-0-1.local.aurafriday.com:31173/messages/?session_id=06aa9c716aca4c6f8c355594f433922a', 'sessionId': None, 'reconnectDelay': 1000, 'reconnectTimer': None, 'lastRequestId': '11111111-b222-4333-8444-555555555555'}}}, 'jsonrpc': '2.0', 'id': '11111111-b222-4333-8444-555555555555'}, 'session_id': '06aa9c716aca4c6f8c355594f433922a'}


    except Exception as e:
        error_msg = f"Error processing registration request: {str(e)}"
        MCPLogger.log("REMOTE", f"Error: {error_msg}\n"+traceback.format_exc())
        return create_error_response(error_msg)


# Convert a remote-tools schema into our compressed-wrapped equivalent.
def compress_tool_definition(registration_data: Dict) -> Dict:
    """Convert a remote tool's registration data into a compressed wrapped tool definition.
    
    Args:
        registration_data: Complete registration dict with tool_name, description, parameters, etc.
        
    Returns:
        Wrapped tool definition suitable for MCP server registration
    """
    # Constants
    
    # Extract fields from registration data
    tool_name = registration_data.get("tool_name", "unknown_tool_name") # e.g. browser
    original_description = registration_data.get("description", "(description missing)")
    readme_field = registration_data.get("readme") # e.g. Read from and perform actions using the users actual desktop web browser.\n- use this anytime a user request can be solved using their local chromium-based browser or current sessions/accounts/credentials/cookies.
    original_parameters = registration_data.get("parameters", {})
    
    # Determine AI-facing description (use readme if provided, otherwise generate default)
    if readme_field:
        ai_description = readme_field.strip() # e.g.
    else:
        ai_description = f'Use this tool when you need to access {tool_name} functionality'
    
    # Generate parameter examples from original schema
    properties = original_parameters.get("properties", {})
    required = original_parameters.get("required", [])
    
    param_examples = []
    for prop_name, prop_schema in properties.items():
        prop_type = prop_schema.get('type', 'string')
        prop_desc = prop_schema.get('description', '')
        
        if prop_type == 'string':
            example_value = f'"example_{prop_name}"'
        elif prop_type == 'number' or prop_type == 'integer':
            example_value = '123'
        elif prop_type == 'boolean':
            example_value = 'true'
        elif prop_type == 'array':
            example_value = '["item1", "item2"]'
        elif prop_type == 'object':
            example_value = '{}'
        else:
            example_value = f'"example_{prop_name}"'
        
        required_marker = " // REQUIRED" if prop_name in required else ""
        param_examples.append(f'       "{prop_name}": {example_value}{required_marker}  // {prop_desc}')
    
    param_section = ',\n'.join(param_examples) if param_examples else '       // No additional parameters'
    
    # Create wrapped tool definition
    wrapped_tool = {
        "name": tool_name,
        "description": ai_description,
        "parameters": {
            "properties": {
                "input": {
                    "type": "object",
                    "description": f"All tool parameters are passed in this single dict. Use {{\"input\":{{\"operation\":\"readme\"}}}} to get full documentation, parameters, and an unlock token."
                }
            },
            "required": [],
            "type": "object"
        },
        "synthetic_parameters": {
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["readme", "execute"],
                    "description": "Operation to perform"
                },
                "tool_unlock_token": {
                    "type": "string",
                    "description": f"Security token, {TEST_TOKEN}, obtained from readme operation"
                }
            },
            "required": ["operation", "tool_unlock_token"],
            "type": "object"
        },
        "original_parameters": original_parameters,  # Store for validation
        "readme": f"""## Available Operations

## Usage-Safety Token System
This tool uses an hmac-based token system to ensure callers fully understand all details of
using this tool, on every call. The token is specific to this installation, user, and code version.

Your tool_unlock_token for this installation is: {TEST_TOKEN}

You MUST include tool_unlock_token in the input dict for all operations except readme.

## Input Structure
All parameters are passed in a single 'input' dict:

1. For this documentation:
   {{
     "input": {{"operation": "readme"}}
   }}

2. For executing the tool:
   {{
     "input": {{
       "operation": "execute", 
       "tool_unlock_token": "{TEST_TOKEN}",
       ... original tool parameters ...
     }}
   }}

## Original Tool Documentation
{original_description}

## Execute Operation Parameters
When using operation="execute", include the original tool parameters:

{{
  "input": {{
    "operation": "execute",
    "tool_unlock_token": "{TEST_TOKEN}",
{param_section}
  }}
}}
"""
    }
    
    return wrapped_tool



def register_tool(input_param: Dict) -> Dict:  # REMOTE handle_remote input_param: {'input': {'operation': 'register', 'tool_name': 'browser', 'description': 'Browser control tool that allows the MCP server to interact with web pages, navigate, click elements, extract content, and perform other browser automation tasks through the MCP Link extension.', 'readme': 'Read from and perform actions using the users actual desktop web browser.\n- use this anytime a user request can be solved using their local chromium-based browser.', 'parameters': {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['navigate', 'click', 'extract_text', 'extract_html', 'scroll', 'wait', 'screenshot', 'evaluate_js'], 'description': 'The browser action to perform'}, 'url': {'type': 'string', 'description': "URL to navigate to (required for 'navigate' action)"}, 'selector': {'type': 'string', 'description': "CSS selector for element to interact with (required for 'click', 'extract_text' actions)"}, 'javascript_code': {'type': 'string', 'description': "JavaScript code to evaluate in the page context (required for 'evaluate_js' action)"}, 'wait_timeout_ms': {'type': 'integer', 'default': 5000, 'description': "Maximum time to wait in milliseconds (for 'wait' action or element waiting)"}, 'scroll_direction': {'type': 'string', 'enum': ['up', 'down', 'top', 'bottom'], 'default': 'down', 'description': "Direction to scroll (for 'scroll' action)"}}, 'required': ['action']}, 'callback_endpoint': 'chrome-extension://browser-tool-callback', 'TOOL_API_KEY': 'mcp_link_extension_browser_tool_auth_key_placeholder'}, 'handler_info': {'tool_name': 'remote', 'session_id': '65fa873198b74a3fbaa83de6e5c69a77', 'request_id': 'f30301f9-c418-441e-a96f-ca56e71dc8dd', 'client': <easy_mcp.server.MCPSession object at 0x0000019A763639D0>, 'responder': <easy_mcp.server.MCPServer object at 0x0000019A76377380>}} 
    """Handle tool registration via MCP interface.
    
    Args:
        input_param: Dictionary containing registration parameters
        
    Returns:
        Dict containing either success confirmation or error information
    """
    try:
        # Pop off synthetic handler_info parameter early (before validation)
        MCPLogger.log("REMOTE", f"register_tool: {input_param}")

        handler_info = input_param.pop('handler_info', {}) if isinstance(input_param, dict) else {}   # {'tool_name': 'remote', 'session_id': '65fa873198b74a3fbaa83de6e5c69a77', 'request_id': 'f30301f9-c418-441e-a96f-ca56e71dc8dd', 'client': <easy_mcp.server.MCPSession object at 0x0000019A763639D0>, 'responder': <easy_mcp.server.MCPServer object at 0x0000019A76377380>}

        # Extract the actual parameters from the "input" wrapper
        if isinstance(input_param, dict) and "input" in input_param:
            actual_params = input_param["input"]
        else:
            return create_error_response("Invalid input format. Expected dictionary with 'input' key containing tool parameters.")
        
        # Validate operation parameter
        operation = actual_params.get("operation")
        if operation != "register":
            return create_error_response(f"Invalid operation: '{operation}'. Only 'register' operation is supported.")

        # Validate required parameters
        required_params = ["tool_name", "description", "parameters", "callback_endpoint", "TOOL_API_KEY"]
        for param in required_params:
            if param not in actual_params:
                return create_error_response(f"Missing required parameter: {param}")
        
        # Extract parameters from actual_params instead of input_param
        base_tool_name = actual_params.get("tool_name")
        description = actual_params.get("description")
        parameters = actual_params.get("parameters")
        callback_endpoint = actual_params.get("callback_endpoint")
        api_key = actual_params.get("TOOL_API_KEY")
        
        # Basic validation
        if not isinstance(base_tool_name, str) or not base_tool_name.strip():
            return create_error_response("tool_name must be a non-empty string")
        
        if not isinstance(description, str) or not description.strip():
            return create_error_response("description must be a non-empty string")
        
        if not isinstance(parameters, dict):
            return create_error_response("parameters must be a valid JSON object/dictionary")
        
        if not isinstance(callback_endpoint, str) or not callback_endpoint.strip():
            return create_error_response("callback_endpoint must be a non-empty string")
        
        if not isinstance(api_key, str) or not api_key.strip():
            return create_error_response("TOOL_API_KEY must be a non-empty string")

        # Check and cleanup any existing tools with the same name that have dead connections
        cleaned_tool_name = base_tool_name.strip()
        if cleaned_tool_name in registered_tools:
            MCPLogger.log("REMOTE", f"Tool {cleaned_tool_name} already exists, checking if connection is still alive...")
            
            # Get the existing tool's session info
            existing_tool_info = registered_tools[cleaned_tool_name]
            existing_session_id = existing_tool_info.get('handler_info', {}).get('session_id')
            
            if existing_session_id:
                # Get server instance to check connection
                server = get_server()
                if server and existing_session_id in server.active_sessions:
                    existing_session = server.active_sessions[existing_session_id]
                    
                    # Check if the connection is still alive
                    if not existing_session.is_socket_connected():
                        MCPLogger.log("REMOTE", f"Existing tool {cleaned_tool_name} has dead connection, removing it...")
                        
                        # Remove the old tool with dead connection
                        del registered_tools[cleaned_tool_name]
                        
                        # Remove from server's tool_handlers
                        if cleaned_tool_name in server.tool_handlers:
                            del server.tool_handlers[cleaned_tool_name]
                            MCPLogger.log("REMOTE", f"Removed dead tool {cleaned_tool_name} from server handlers")
                        
                        MCPLogger.log("REMOTE", f"Successfully cleaned up dead tool {cleaned_tool_name}")
                    else:
                        MCPLogger.log("REMOTE", f"Existing tool {cleaned_tool_name} connection is still alive, will resolve naming conflict")
                else:
                    # Session not found in active_sessions, it's dead
                    MCPLogger.log("REMOTE", f"Existing tool {cleaned_tool_name} session {existing_session_id} not found in active sessions, removing it...")
                    
                    # Remove the old tool with dead session
                    del registered_tools[cleaned_tool_name]
                    
                    # Remove from server's tool_handlers
                    server = get_server()
                    if server and cleaned_tool_name in server.tool_handlers:
                        del server.tool_handlers[cleaned_tool_name]
                        MCPLogger.log("REMOTE", f"Removed dead tool {cleaned_tool_name} from server handlers")
                    
                    MCPLogger.log("REMOTE", f"Successfully cleaned up dead tool {cleaned_tool_name} (session not found)")
            else:
                MCPLogger.log("REMOTE", f"Existing tool {cleaned_tool_name} has no session info, removing it...")
                
                # Remove the old tool with no session info
                del registered_tools[cleaned_tool_name]
                
                # Remove from server's tool_handlers
                server = get_server()
                if server and cleaned_tool_name in server.tool_handlers:
                    del server.tool_handlers[cleaned_tool_name]
                    MCPLogger.log("REMOTE", f"Removed tool {cleaned_tool_name} with no session info from server handlers")

        # Resolve naming conflicts (after cleanup, this might not be needed)
        final_tool_name = resolve_tool_name_conflict(cleaned_tool_name)
        
        if COMPRESS_TOOL_DEFINITIONS:
            MCPLogger.log(f"REMOTE", f"compressing tool definition {YEL}{actual_params}{NORM}")
            final_params = compress_tool_definition(actual_params) # e.g.
            temp_readme=final_params.get("readme")
            # un-swap before re-swap
            #final_params["readme"]=final_params.get("description")
            #final_params["description"]=temp_readme
            MCPLogger.log("REMOTE", f"compressed tool definition to {final_params}")
        else:
            final_params = actual_params

        # Register the tool in our internal registry
        registered_tools[final_tool_name] = {
            "description": final_params.get("description").strip(),
            "parameters": final_params.get("parameters"),
            "synthetic_parameters": final_params.get("synthetic_parameters"),
            "callback_endpoint": callback_endpoint.strip(),
            "api_key": api_key.strip(),
            "readme": final_params.get("readme"),
            "registered_at": time.time(),
            "handler_info": handler_info # is session_id in here is the client, not the tool-connection, from cursor???
            #"session_id": 2
        }
        
        # Get the server instance and register the tool with it
        server = get_server()
        if server:
            # Register cleanup callback on first tool registration
            global _cleanup_callback_registered
            if not _cleanup_callback_registered:
                try:
                    server.register_session_cleanup_callback(cleanup_tools_for_session)
                    _cleanup_callback_registered = True
                    MCPLogger.log("REMOTE", "Successfully registered session cleanup callback")
                except Exception as e:
                    MCPLogger.log("REMOTE", f"Error registering session cleanup callback: {str(e)}")
            
            # Create a handler for this remote tool
            handler = create_remote_tool_handler(final_tool_name, callback_endpoint.strip(), api_key.strip())
            
            # Register with the MCP server so it appears in tools/list
            server.register_tool(
                name=final_tool_name,
                description=registered_tools[final_tool_name].get("description").strip(),
                input_schema=registered_tools[final_tool_name].get("parameters"),
                handler=handler
            )

            # "tool_unlock_token": { "type": "string", "description": "Security token obtained from readme documentation" }
            # "parameters": { "properties": { "input": { "type": "object", "description": "All tool parameters are passed in this single dict. Use {\"input\":{\"readme\":true}} to get full documentation, parameters, and an unlock token." } }, "required": [], "type": "object" },
            
            MCPLogger.log("REMOTE", f"Successfully registered tool with MCP server: {final_tool_name} rego={registered_tools[final_tool_name]}")
        else:
            MCPLogger.log("REMOTE", f"Warning: No server instance available, tool {final_tool_name} only stored in internal registry")
        
        # Log successful registration
        MCPLogger.log("REMOTE", f"Successfully registered tool: {final_tool_name}")                          # browser
        MCPLogger.log("REMOTE", f"  Description: {registered_tools[final_tool_name].get('description')[:100]}...")
        MCPLogger.log("REMOTE", f"  Parameters: {registered_tools[final_tool_name].get('parameters')}")
        MCPLogger.log("REMOTE", f"  Callback: {callback_endpoint} full={registered_tools[final_tool_name]}") # full={'description': 'Browser control tool that allows the MCP server to interact with web pages, navigate, click elements, extract content, and perform other browser automation tasks through the MCP Link extension.', 'parameters': {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['navigate', 'click', 'extract_text', 'extract_html', 'scroll', 'wait', 'screenshot', 'evaluate_js'], 'description': 'The browser action to perform'}, 'url': {'type': 'string', 'description': "URL to navigate to (required for 'navigate' action)"}, 'selector': {'type': 'string', 'description': "CSS selector for element to interact with (required for 'click', 'extract_text' actions)"}, 'javascript_code': {'type': 'string', 'description': "JavaScript code to evaluate in the page context (required for 'evaluate_js' action)"}, 'wait_timeout_ms': {'type': 'integer', 'default': 5000, 'description': "Maximum time to wait in milliseconds (for 'wait' action or element waiting)"}, 'scroll_direction': {'type': 'string', 'enum': ['up', 'down', 'top', 'bottom'], 'default': 'down', 'description': "Direction to scroll (for 'scroll' action)"}}, 'required': ['action']}, 'callback_endpoint': 'chrome-extension://browser-tool-callback', 'api_key': 'mcp_link_extension_browser_tool_auth_key_placeholder', 'readme': 'Read from and perform actions using the users actual desktop web browser.\n- use this anytime a user request can be solved using their local chromium-based browser.', 'registered_at': 1750508378.0326962, 'handler_info': {'tool_name': 'remote', 'session_id': '2224b155a8464d2081c65fabcd941981', 'request_id': '79380100-4901-4878-96dc-18571966db90', 'client': <easy_mcp.server.MCPSession object at 0x0000023BC72679D0>, 'responder': <easy_mcp.server.MCPServer object at 0x0000023BC7277380>}}
        MCPLogger.log("REMOTE", f"  Total registered tools: {len(registered_tools)}")

        # Trigger Cursor IDE to reconnect so it can see the newly registered tool
        trigger_cursor_reconnect_for_tool_changes()
        
        # Prepare success response
        response_text = f"Successfully registered tool: {final_tool_name}"
        if final_tool_name != base_tool_name:
            response_text += f" (renamed from {base_tool_name} due to naming conflict)"
        
        return {
            "content": [{"type": "text", "text": response_text}],
            "isError": False
        }
            
    except Exception as e:
        error_msg = f"Error processing registration request: {str(e)}"
        MCPLogger.log("REMOTE", f"Error: {error_msg}\n"+traceback.format_exc())
        return create_error_response(error_msg)

# Map of tool names to their handlers
HANDLERS = {
    "remote": handle_remote
}

# Note: Session cleanup callback registration happens in __init__.py during tool discovery
# because this module's bottom section doesn't execute during normal tool loading


""" Tools calls look like this:-

2025-06-20 10:50:19.383 [PID:23540|TID:39168] Request from ('127.0.0.1', 55963) < Method: POST Path: /messages/?session_id=2ac6884a5bde4c05bb3247e42e7ff134\nHeaders: 
{'host': '127-0-0-1.local.aurafriday.com:31173', 'connection': 'keep-alive', 'content-type': 'application/json', 'accept': '*/*', 'accept-language': '*', 'sec-fetch-mode': 'cors', 'user-agent': 'node', 'accept-encoding': 'br, gzip, deflate', 'content-length': '136'}\n
Body length: 136\n
Body: b'{"method":"tools/call","params":{"name":"browser","arguments":{"action":"navigate","url":"https://example.com"}},"jsonrpc":"2.0","id":2}'

2025-06-20 10:50:19.384 [PID:23540|TID:39168] JSONRPC Request session=2ac6884a5bde4c05bb3247e42e7ff134, method=tools/call, id=2
2025-06-20 10:50:19.384 [PID:23540|TID:39168] REMOTE Tool browser args: {'action': 'navigate', 'url': 'https://example.com', 'handler_info': {'browser': 'browser'}}
2025-06-20 10:50:19.385 [PID:23540|TID:39168] SSE Message to ('127.0.0.1', 55921) > data: {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "work-in-progress (this tool is not yet fully implemented)"}], "isError": false}}\r\n\r\n
2025-06-20 10:50:19.386 [PID:23540|TID:39168] Response  to ('127.0.0.1', 55963) > b'HTTP/1.1 202 Accepted\r\nContent-Type: text/plain\r\nContent-Length: 0\r\nConnection: close\r\nAccess-Control-Allow-Origin: null\r\nAccess-Control-Allow-Methods: GET, POST, PUT, DELETE, PATCH, OPTIONS\r\nAccess-Control-Allow-Headers: *\r\nAccess-Control-Allow-Credentials: true\r\nAccess-Control-Max-Age: 86400\r\n\r\n'

"""
