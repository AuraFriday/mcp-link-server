"""
File: ragtag/tools/user.py
Project: Aura Friday MCP-Link Server
Component: User Interface Tool
Author: Christopher Nathan Drake (cnd)

Tool implementation for displaying HTML pop-up windows to communicate with users.
Leverages the friday.py Qt infrastructure to show interactive HTML content using QWebEngineView.

VERSION: 2025.09.13.001 - Proxy to friday.py UIService (No Direct Qt)

Copyright: © 2025 Christopher Nathan Drake. All rights reserved.
SPDX-License-Identifier: Proprietary
"signature": "JƲ৭ЕY𝟧MꓑƼDlþ𝕌𝟑ģКϹᎻОᛕďոƍµMþPRеОgꓔÞνDģᏴcDᗅ𝟦ВÐНƋɋɋ𝟛ꓠU5ⲦfrƻYƘɯuᴍiiΟP9𝟣𝛢𝟟ƙ𐐕ꓧĸoʋᎬΒН𐓒ꓝɋꙄ𝟥ŧωꓑоꜱnϜtᎠОϜɌƤᗅАkΡƬ𝙰6ƼMР𝟛ցȜ3"
"signdate": "2025-11-24T12:18:44.953Z",
"""

# Version tracking for debugging MCP integration
USER_TOOL_VERSION = "2025.09.13.009"
USER_TOOL_VERSION_DESC = "No Friday.py Imports - Uses sys.modules Queue Registry (Native Python)"

import json
import queue
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from easy_mcp.server import MCPLogger, get_tool_token
from typing import Dict, List, Optional, Union, BinaryIO, Tuple, Any

@dataclass
class UIRequest:
    """Message structure for UI requests to Friday.py Qt main thread via sys.modules registry."""
    operation: str
    data: Dict[str, Any]
    reply_queue: queue.Queue

# Constants
TOOL_LOG_NAME = "USER"

# Module-level token generated once at import time
TOOL_UNLOCK_TOKEN = get_tool_token(__file__)

# Tool definitions
TOOLS = [
    {
        "name": "user",
        # The "description" key is the only thing that persists in the AI context at all times.
        # To prevent context wastage, agents use `readme` to get the full documentation when needed.
        # We have a called_readme_operation_in_user parameter to block them bypassing the `readme` operation.
        # Keep this description as brief as possible, but it must include everything an AI needs to know
        # to work out if it should use this tool, and needs to clearly tell the AI to use
        # the readme operation to find out how to do that.
        # - Creates beautiful HTML interfaces using the Qt WebEngine browser
        # - Works on Windows, Mac, and Linux through the friday.py Qt infrastructure
        "description": """Show HTML pop-up windows to communicate with users.
- Use this when you need to collect API keys, show forms, display rich content, get user input, etc.
""",
        # Standard MCP parameters - simplified to single input dict
        "parameters": {
            "properties": {
                "input": {
                    "type": "object",
                    "description": "All tool parameters are passed in this single dict. Use {\"input\":{\"operation\":\"readme\"}} to get full documentation, parameters, and an unlock token."
                }
            },
            "required": [],
            "type": "object"
        },
        # Actual tool parameters - revealed only after readme call
        "real_parameters": {
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["readme", "show_popup", "show_dialog", "test_queue", "collect_api_key", "show_toast", "send_message", "check_messages", "show_dashboard", "hide_dashboard", "get_message_history", "clear_messages"],
                    "description": "Operation to perform"
                },
                "html": {
                    "type": "string",
                    "description": "HTML content to display in the popup/dialog window (mutually exclusive with url)"
                },
                "url": {
                    "type": "string",
                    "description": "URL to load in the window (mutually exclusive with html)"
                },
                "title": {
                    "type": "string",
                    "description": "Window title (optional, defaults to 'User Interface')",
                    "default": "User Interface"
                },
                "width": {
                    "type": "integer",
                    "description": "Window width in pixels (optional, defaults to 600)",
                    "default": 600
                },
                "height": {
                    "type": "integer", 
                    "description": "Window height in pixels (optional, defaults to 400)",
                    "default": 400
                },
                "modal": {
                    "type": "boolean",
                    "description": "Whether the dialog should be modal (blocks other windows, optional, defaults to true)",
                    "default": True
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds to wait for user interaction (optional, 0 = no timeout)",
                    "default": 0
                },
                "message": {
                    "type": "string",
                    "description": "Test message for queue communication testing (used with test_queue operation)",
                    "default": "Hello from user.py"
                },
                "center_on_screen": {
                    "type": "boolean",
                    "description": "Center window on screen (optional, defaults to true)",
                    "default": True
                },
                "always_on_top": {
                    "type": "boolean", 
                    "description": "Keep window above other windows (optional, defaults to true)",
                    "default": True
                },
                "bring_to_front": {
                    "type": "boolean",
                    "description": "Force window to foreground (optional, defaults to true)", 
                    "default": True
                },
                "auto_resize": {
                    "type": "boolean",
                    "description": "Automatically resize window to fit content (optional, defaults to false)",
                    "default": False
                },
                "resizable": {
                    "type": "boolean",
                    "description": "Allow user to resize the window (optional, defaults to false)",
                    "default": False
                },
                "wait_for_response": {
                    "type": "boolean",
                    "description": "Wait for user to close window before returning (optional, defaults to true). Set to false for 'fire and forget' mode where the window opens but AI continues immediately.",
                    "default": True
                },
                "service_name": {
                    "type": "string",
                    "description": "Name of the service requiring an API key (used with collect_api_key operation)",
                    "default": "API Service"
                },
                "service_url": {
                    "type": "string",
                    "description": "URL where users can obtain an API key (used with collect_api_key operation)",
                    "default": ""
                },
                "message": {
                    "type": "string",
                    "description": "Toast notification message text (used with show_toast operation)"
                },
                "level": {
                    "type": "string",
                    "enum": ["info", "warning", "error", "success"],
                    "description": "Toast notification level/severity (used with show_toast operation)",
                    "default": "info"
                },
                "content": {
                    "type": "string",
                    "description": "Message content text (used with send_message operation)"
                },
                "msg_type": {
                    "type": "string",
                    "enum": ["question", "status", "notification", "response"],
                    "description": "Type of message (used with send_message operation)",
                    "default": "status"
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "normal", "high", "critical"],
                    "description": "Message priority level (used with send_message operation)",
                    "default": "normal"
                },
                "requires_response": {
                    "type": "boolean",
                    "description": "Whether this message requires a user response (used with send_message operation)",
                    "default": False
                },
                "show_dashboard": {
                    "type": "boolean",
                    "description": "Show dashboard window if hidden (used with send_message operation)",
                    "default": True
                },
                "mark_as_read": {
                    "type": "boolean",
                    "description": "Mark retrieved messages as read (used with check_messages operation)",
                    "default": True
                },
                "filter_type": {
                    "type": "string",
                    "description": "Filter messages by type (used with check_messages operation)"
                },
                "since_timestamp": {
                    "type": "number",
                    "description": "Only get messages after this timestamp (used with check_messages operation)"
                },
                "tool_unlock_token": {
                    "type": "string",
                    "description": "Security token, " + TOOL_UNLOCK_TOKEN + ", obtained from readme operation, or re-provided any time the AI lost context or gave a wrong token"
                }
            },
            "required": ["operation", "tool_unlock_token"],
            "type": "object"
        },

        # Detailed documentation - obtained via "input":"readme" initial call (and in the event any call arrives without a valid token)
        # It should be verbose and clear with lots of examples so the AI fully understands
        # every feature and how to use it.

        "readme": """
Display HTML popup windows to interact with users.

This tool creates beautiful, interactive HTML interfaces using Qt WebEngine that work
across Windows, Mac, and Linux. Perfect for collecting API keys, showing forms,
displaying rich content, or getting any kind of user input.

## Usage-Safety Token System
This tool uses an hmac-based token system to ensure callers fully understand all details of
using this tool, on every call. The token is specific to this installation, user, and code version.

Your tool_unlock_token for this installation is: """ + TOOL_UNLOCK_TOKEN + """

You MUST include tool_unlock_token in the input dict for all operations.

## Operations Available

### 1. show_popup - Non-modal popup window
Creates a non-modal popup that doesn't block other application windows.
Good for displaying information or simple forms.

### 2. show_dialog - Modal dialog window  
Creates a modal dialog that blocks interaction with other windows until closed.
Best for critical input like API keys or confirmation dialogs.

### 3. show_toast - Toast notification message
Shows a brief notification message in the system tray area.
Perfect for quick status updates, confirmations, or alerts that don't require user interaction.
Messages appear in the "Server Status" menu accessible from the system tray icon.

### 4. send_message - Send async message to user (NON-BLOCKING)
Sends a message to the user via a persistent dashboard window WITHOUT blocking AI execution.
Perfect for status updates, progress reports, or questions while the AI continues working.
The dashboard appears in the top-right corner with color-coded messages.

### 5. check_messages - Check for user replies (NON-BLOCKING)
Checks if the user has sent any messages back to the AI via the dashboard.
Returns only NEW unread messages by default. Use this periodically to enable co-working!

### 6. show_dashboard - Show the message dashboard window
Displays the persistent message dashboard if it's hidden.
The dashboard shows all AI↔User message history with timestamps and priorities.

### 7. hide_dashboard - Hide the message dashboard window
Hides the dashboard window (messages are still queued and accessible).

### 8. get_message_history - Get complete message history
Returns ALL messages (both AI-to-user and user-to-AI) regardless of read status.
Useful for reviewing the entire conversation history.

### 9. clear_messages - Clear all message queues
Clears all messages from the queue (use with caution!).

## Parameters

### For show_popup and show_dialog operations:
- **html** (required): Full HTML content including DOCTYPE, head, body
- **title** (optional): Window title (default: "User Interface")
- **width** (optional): Window width in pixels (default: 600)
- **height** (optional): Window height in pixels (default: 400)  
- **modal** (optional): true = modal dialog, false = popup (default: true)
- **timeout** (optional): Seconds to wait for user input, 0 = no timeout (default: 0)
- **center_on_screen** (optional): true = center window on screen (default: true)
- **always_on_top** (optional): true = keep window above other windows (default: true)
- **bring_to_front** (optional): true = force window to foreground (default: true)
- **auto_resize** (optional): true = automatically resize window to fit content (default: false)

### For show_toast operation:
- **message** (required): The text message to display in the toast notification
- **level** (optional): Notification severity level - "info", "warning", "error", or "success" (default: "info")

### For send_message operation:
- **content** (required): The message text to send to the user
- **msg_type** (optional): Message type - "question", "status", "notification", or "response" (default: "status")
- **priority** (optional): Priority level - "low", "normal", "high", or "critical" (default: "normal")
- **requires_response** (optional): Whether this message requires a user response (default: false)
- **show_dashboard** (optional): Show dashboard window if hidden (default: true)

### For check_messages operation:
- **mark_as_read** (optional): Mark retrieved messages as read so they won't be returned again (default: true)
- **filter_type** (optional): Filter messages by type (e.g., "response", "question")
- **since_timestamp** (optional): Only get messages after this timestamp

### For show_dashboard, hide_dashboard, get_message_history, clear_messages operations:
- No additional parameters required (just operation and tool_unlock_token)

## Window Sizing Guidelines
**IMPORTANT:** Qt WebEngine adds window chrome (title bar + borders) to your requested size:
- **Actual width = requested width + 16px**
- **Actual height = requested height + 39px**

Your HTML content gets the EXACT dimensions you request - the chrome is added on top.

**Sizing Best Practices:**
- **Always overestimate rather than underestimate** - scrollbars look unprofessional
- **Add 50-80px buffer** beyond your estimated content height for safety
- **Account for all spacing:** body padding + container padding + margins + content + buttons
- **Common sizes that work well:**
  - Simple forms: 400-500px height (not 300-400px)
  - API key dialogs: 450-550px height (not 350-450px)  
  - Information displays: 350-450px height (not 250-350px)
  - Complex forms: 600-800px height (not 500-700px)
- **Rule of thumb:** If you think you need 300px, request 400px
- **Test approach:** Create HTML, estimate height, add 100px, test, then reduce if too large

## JavaScript Bridge
Your HTML can communicate back to the tool using:
```javascript
// Send data back to the AI
window.userResponse = {"status": "success", "data": {"api_key": "sk-1234..."}};
window.close(); // Close the window

// Or send error/cancel
window.userResponse = {"status": "cancelled", "message": "User cancelled"};
window.close();
```

## Window Management Features
**Inspired by native Windows applications:**
- **center_on_screen**: Automatically centers window on user's screen (default: true)
- **always_on_top**: Keeps window above all other applications (default: true)  
- **bring_to_front**: Forces window to foreground when shown (default: true)
- **auto_resize**: Automatically resize window to fit content perfectly (default: false)
- **Modal dialogs**: Block interaction with other windows until closed (modal: true)
- **Precise sizing**: Content gets exact requested dimensions + consistent chrome (+16px width, +39px height)

### ⚠️ Windows Platform Limitations
**CRITICAL for AI agents:** On Windows systems, the `bring_to_front` parameter typically does NOT work reliably due to OS-level focus stealing prevention. Windows prevents applications from forcibly stealing focus from the user's current active window.

**Recommended approach for Windows:**
- Use `always_on_top: true` for reliable popup visibility
- Do not depend on `bring_to_front` working on Windows
- `always_on_top` ensures the window appears above all other applications regardless of focus

**Example for reliable popup visibility on Windows:**
```json
{
  "always_on_top": true,
  "bring_to_front": false
}
```

### Auto-Resize Feature
When `auto_resize: true`, the window starts larger than requested, measures the actual content size, then shrinks to fit perfectly. This eliminates scrollbar-induced layout changes!
- Window starts at 2x requested height to avoid scrollbars
- JavaScript measures the actual content dimensions  
- Window shrinks to fit content + padding
- Window re-centers automatically after resize
- Avoids scrollbar → text wrap → height change cascade

## HTML Templates

### API Key Collection Example:
```html
<!DOCTYPE html>
<html>
<head>
    <title>API Key Required</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; 
               padding: 30px; background: #f5f5f5; margin: 0; }
        .container { background: white; padding: 30px; border-radius: 8px; 
                    box-shadow: 0 2px 10px rgba(0,0,0,0.1); max-width: 400px; margin: 0 auto; }
        h2 { color: #333; margin-top: 0; }
        input[type="password"] { width: 100%; padding: 12px; border: 1px solid #ddd; 
                                border-radius: 4px; font-size: 14px; margin: 10px 0; }
        button { background: #007AFF; color: white; border: none; padding: 12px 24px; 
                border-radius: 4px; cursor: pointer; margin-right: 10px; }
        button:hover { background: #0056CC; }
        .cancel { background: #666; } .cancel:hover { background: #333; }
    </style>
</head>
<body>
    <div class="container">
        <h2>🔑 OpenAI API Key Required</h2>
        <p>Please enter your OpenAI API key to continue:</p>
        <input type="password" id="apiKey" placeholder="sk-..." autofocus>
        <div style="margin-top: 20px;">
            <button onclick="submit()">Submit</button>
            <button class="cancel" onclick="cancel()">Cancel</button>
        </div>
    </div>
    
    <script>
        function submit() {
            const key = document.getElementById('apiKey').value.trim();
            if (!key) return alert('Please enter an API key');
            if (!key.startsWith('sk-')) return alert('Invalid API key format');
            
            window.userResponse = {"status": "success", "data": {"api_key": key}};
            window.close();
        }
        
        function cancel() {
            window.userResponse = {"status": "cancelled"};
            window.close();
        }
        
        // Submit on Enter key
        document.getElementById('apiKey').addEventListener('keypress', function(e) {
            if (e.key === 'Enter') submit();
        });
    </script>
</body>
</html>
```

### Simple Confirmation Dialog:
```html
<!DOCTYPE html>
<html>
<head>
    <title>Confirmation</title>
    <style>
        body { font-family: system-ui; padding: 20px; text-align: center; }
        .icon { font-size: 48px; margin-bottom: 20px; }
        button { padding: 10px 20px; margin: 0 5px; border: none; border-radius: 4px; cursor: pointer; }
        .yes { background: #28a745; color: white; } .no { background: #dc3545; color: white; }
    </style>
</head>
<body>
    <div class="icon">⚠</div>
    <h2>Delete all files?</h2>
    <p>This action cannot be undone.</p>
    <button class="yes" onclick="respond(true)">Yes, Delete</button>
    <button class="no" onclick="respond(false)">Cancel</button>
    
    <script>
        function respond(confirmed) {
            window.userResponse = {"status": "success", "data": {"confirmed": confirmed}};
            window.close();
        }
    </script>
</body>
</html>
```

## Input Examples

### 1. Get README documentation:
```json
{
  "input": {"operation": "readme"}
}
```

### 2. Show API key collection dialog:
```json
{
  "input": {
    "operation": "show_dialog",
    "html": "<!DOCTYPE html><html>...[full HTML as shown above]...</html>",
    "title": "API Key Required",
    "width": 500,
    "height": 300,
    "modal": true,
    "timeout": 120,
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```

### 3. Show information popup:
```json
{
  "input": {
    "operation": "show_popup", 
    "html": "<!DOCTYPE html><html><body><h2>Success!</h2><p>Your settings have been saved.</p><script>setTimeout(() => window.close(), 3000);</script></body></html>",
    "title": "Success",
    "width": 400,
    "height": 200,
    "modal": false,
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```

### 4. Show toast notification:
```json
{
  "input": {
    "operation": "show_toast",
    "message": "File uploaded successfully!",
    "level": "success",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```

### 5. Send async message to user (NON-BLOCKING):
```json
{
  "input": {
    "operation": "send_message",
    "content": "Starting Android app setup...\\n\\nStep 1: Installing Android Studio\\nStep 2: Configuring SDK",
    "msg_type": "status",
    "priority": "normal",
    "show_dashboard": true,
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```

### 6. Check for user replies (NON-BLOCKING):
```json
{
  "input": {
    "operation": "check_messages",
    "mark_as_read": true,
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```

Returns:
```json
{
  "messages": [
    {
      "id": "msg-uuid",
      "timestamp": 1763524358.29,
      "direction": "user_to_ai",
      "type": "response",
      "priority": "normal",
      "content": "Looks good! Continue with step 2",
      "requires_response": false,
      "status": "pending"
    }
  ],
  "count": 1
}
```

### 7. Get complete message history:
```json
{
  "input": {
    "operation": "get_message_history",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```

### 8. Show/hide dashboard:
```json
{
  "input": {
    "operation": "show_dashboard",
    "tool_unlock_token": """ + f'"{TOOL_UNLOCK_TOKEN}"' + """
  }
}
```

## Async Messaging Co-Working Pattern

**Perfect for long-running tasks where you want user feedback!**

```python
# Send status update
send_message("Starting Android setup...")

# Do some work
install_android_studio()

# Check if user sent feedback
messages = check_messages()
if messages:
    for msg in messages:
        print(f"User says: {msg['content']}")
        # Respond to user
        send_message(f"Thanks! {msg['content']}")

# Continue working
configure_sdk()

# Check again periodically
messages = check_messages()
```

**Key Benefits:**
- ✅ Non-blocking - AI continues working while dashboard is open
- ✅ Bidirectional - User can send messages anytime
- ✅ Persistent - Dashboard stays open, messages accumulate
- ✅ Color-coded - AI messages (blue tint), User messages (green tint)
- ✅ Timestamped - All messages have timestamps and priorities

## Return Values
The tool returns a JSON response with user data:
```json
{
  "status": "success",
  "data": {"api_key": "sk-1234...", "other_field": "value"},
  "window_closed": true
}
```

Or for cancellation/errors:
```json
{
  "status": "cancelled", 
  "message": "User cancelled the dialog"
}
```

## Best Practices
1. Always include complete HTML with DOCTYPE, head, and body
2. Use modern CSS for beautiful, responsive layouts
3. Include proper error handling in JavaScript
4. Set reasonable timeouts for modal dialogs
5. Use semantic HTML and proper form validation
6. Test your HTML in a browser first for complex interfaces

## Common Use Cases
- Collecting API keys and secrets
- User preferences and configuration
- File selection and options
- Confirmation dialogs
- Progress displays with rich formatting
- Multi-step wizards
- Data entry forms
- Error displays with actionable options
- Quick status notifications (toast messages)
- Background task completion alerts
- Non-intrusive success/error messages
"""
    }
]

def validate_parameters(input_param: Dict) -> Tuple[Optional[str], Dict]:
    """Validate input parameters against the real_parameters schema.
    
    Args:
        input_param: Input parameters dictionary
        
    Returns:
        Tuple of (error_message, validated_params) where error_message is None if valid
    """
    real_params_schema = TOOLS[0]["real_parameters"]
    properties = real_params_schema["properties"]
    required = real_params_schema.get("required", [])
    
    # For readme operation, don't require token
    operation = input_param.get("operation")
    if operation == "readme":
        required = ["operation"]  # Only operation is required for readme
    
    # Check for unexpected parameters
    expected_params = set(properties.keys())
    provided_params = set(input_param.keys())
    unexpected_params = provided_params - expected_params
    
    if unexpected_params:
        return f"Unexpected parameters provided: {', '.join(sorted(unexpected_params))}. Expected parameters are: {', '.join(sorted(expected_params))}. Please consult the attached doc.", {}
    
    # Check for missing required parameters
    missing_required = set(required) - provided_params
    if missing_required:
        return f"Missing required parameters: {', '.join(sorted(missing_required))}. Required parameters are: {', '.join(sorted(required))}", {}
    
    # Validate types and extract values
    validated = {}
    for param_name, param_schema in properties.items():
        if param_name in input_param:
            value = input_param[param_name]
            expected_type = param_schema.get("type")
            
            # Type validation
            if expected_type == "string" and not isinstance(value, str):
                return f"Parameter '{param_name}' must be a string, got {type(value).__name__}. Please provide a string value.", {}
            elif expected_type == "object" and not isinstance(value, dict):
                return f"Parameter '{param_name}' must be an object/dictionary, got {type(value).__name__}. Please provide a dictionary value.", {}
            elif expected_type == "integer" and not isinstance(value, int):
                return f"Parameter '{param_name}' must be an integer, got {type(value).__name__}. Please provide an integer value.", {}
            elif expected_type == "boolean" and not isinstance(value, bool):
                return f"Parameter '{param_name}' must be a boolean, got {type(value).__name__}. Please provide true or false.", {}
            elif expected_type == "array" and not isinstance(value, list):
                return f"Parameter '{param_name}' must be an array/list, got {type(value).__name__}. Please provide a list value.", {}
            
            # Enum validation
            if "enum" in param_schema:
                allowed_values = param_schema["enum"]
                if value not in allowed_values:
                    return f"Parameter '{param_name}' must be one of {allowed_values}, got '{value}'. Please use one of the allowed values.", {}
            
            validated[param_name] = value
        elif param_name in required:
            # This should have been caught above, but double-check
            return f"Required parameter '{param_name}' is missing. Please provide this required parameter.", {}
        else:
            # Use default value if specified
            default_value = param_schema.get("default")
            if default_value is not None:
                validated[param_name] = default_value
    
    return None, validated

def readme(with_readme: bool = True) -> str:
    """Return tool documentation.
    
    Args:
        with_readme: If False, returns empty string. If True, returns the complete tool documentation.
        
    Returns:
        The complete tool documentation with the readme content as description, or empty string if with_readme is False.
    """
    try:
        if not with_readme:
            return ''
            
        MCPLogger.log(TOOL_LOG_NAME, "Processing readme request")
        return "\n\n" + json.dumps({
            "description": TOOLS[0]["readme"],
            "parameters": TOOLS[0]["real_parameters"] # the caller knows these as the dict that goes inside "input" though
            #"real_parameters": TOOLS[0]["real_parameters"] # the caller knows these as the dict that goes inside "input" though
        }, indent=2)
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Error processing readme request: {str(e)}")
        return ''

def create_error_response(error_msg: str, with_readme: bool = True, include_traceback: bool = True) -> Dict:
    """Log and Create an error response that optionally includes the tool documentation.
    example:   if some_error: return create_error_response(f"some error with details: {str(e)}", with_readme=False)
    """
    MCPLogger.log(TOOL_LOG_NAME, f"Error: {error_msg}")
    
    if include_traceback:
        stack_trace = traceback.format_exc()
        MCPLogger.log(TOOL_LOG_NAME, f"Full stack trace: {stack_trace}")
    
    return {"content": [{"type": "text", "text": f"{error_msg}{readme(with_readme)}"}], "isError": True}

def test_queue_communication(params: Dict) -> Dict:
    """Test queue communication with friday.py without any Qt operations.
    
    Args:
        params: Dictionary containing the operation parameters
        
    Returns:
        Dict containing the response from friday.py or error information
    """
    try:
        # Extract message parameter
        message = params.get("message", "Hello from user.py")
        
        MCPLogger.log(TOOL_LOG_NAME, f"Testing queue communication with message: '{message}'")
        
        # Try to communicate via queue
        try:
            result = _test_queue_message_passing(message)
            return {
                "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                "isError": False
            }
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Queue communication test failed: {str(e)}")
            MCPLogger.log(TOOL_LOG_NAME, f"Stack trace: {traceback.format_exc()}")
            return create_error_response(f"Queue communication test failed: {str(e)}", with_readme=False)
            
    except Exception as e:
        return create_error_response(f"Error processing queue communication test: {str(e)}", with_readme=True)


def _test_queue_message_passing(message: str) -> Dict:
    """Internal function to test queue message passing to friday.py without Qt operations.
    
    Args:
        message: Test message to send
        
    Returns:
        Dict with response from friday.py
    """
    try:
        thread_id = threading.current_thread().ident
        MCPLogger.log(TOOL_LOG_NAME, f"Testing queue message passing: {message} - user.py v{USER_TOOL_VERSION} ({USER_TOOL_VERSION_DESC}) [Thread: {thread_id}]")
        
        # Access friday.py's UI queue via sys.modules registry (no imports needed!)
        try:
            request_queue = sys.modules.get('friday_ui_queue')
            
            if request_queue is None:
                return {"status": "error", "error": "No UI request queue available. Ensure friday.py is running with Qt support."}
                
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Could not access friday.py queue via sys.modules: {e}")
            MCPLogger.log(TOOL_LOG_NAME, f"Registry access error stack trace: {traceback.format_exc()}")
            return {"status": "error", "error": "Could not access friday.py UI queue. Ensure server was started via friday.py."}
        
        # Create a reply queue for this specific request
        reply_queue = queue.Queue()
        
        # Prepare request data (no Qt-related parameters)
        request_data = {
            "message": message,
            "timestamp": time.time(),
            "user_tool_version": USER_TOOL_VERSION
        }
        
        # Create UI request message
        ui_request = UIRequest(
            operation="test_queue",
            data=request_data,
            reply_queue=reply_queue
        )
        
        MCPLogger.log(TOOL_LOG_NAME, f"Sending test_queue request to friday.py queue [Thread: {thread_id}]")
        
        # Send request to friday.py's main thread via queue
        request_queue.put(ui_request)
        
        # Wait for response from reply queue
        max_wait_time = 10  # 10 seconds should be plenty for a simple queue test
        
        try:
            response = reply_queue.get(timeout=max_wait_time)
            return response
            
        except queue.Empty:
            MCPLogger.log(TOOL_LOG_NAME, f"Queue test timed out after {max_wait_time} seconds")
            return {"status": "timeout", "error": f"Queue test timed out after {max_wait_time} seconds"}
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"CRITICAL ERROR in queue test: {str(e)}")
        MCPLogger.log(TOOL_LOG_NAME, f"Complete stack trace: {traceback.format_exc()}")
        return {"status": "error", "error": f"Error in queue test communication: {str(e)}"}


def show_html_window(params: Dict) -> Dict:
    """Show HTML content in a Qt WebEngine window.
    
    Args:
        params: Dictionary containing the operation parameters
        
    Returns:
        Dict containing the user response or error information
    """
    try:
        # Extract required parameters
        html = params.get("html")
        url = params.get("url")
        
        # Must have either html or url, but not both
        if not html and not url:
            return create_error_response("Missing required parameter: must provide either 'html' or 'url'", with_readme=True)
        if html and url:
            return create_error_response("Cannot specify both 'html' and 'url' - choose one", with_readme=True)
        
        if html and not isinstance(html, str):
            return create_error_response(f"Parameter 'html' must be a string, got {type(html).__name__}. Please provide HTML content as a string.", with_readme=True)
        if url and not isinstance(url, str):
            return create_error_response(f"Parameter 'url' must be a string, got {type(url).__name__}.", with_readme=True)
        
        # Extract optional parameters with defaults
        title = params.get("title", "User Interface")
        width = params.get("width", 600)
        height = params.get("height", 400)
        resizable = params.get("resizable", False)
        modal = params.get("modal", True)
        timeout = params.get("timeout", 0)
        center_on_screen = params.get("center_on_screen", True)
        always_on_top = params.get("always_on_top", True)
        bring_to_front = params.get("bring_to_front", True)
        auto_resize = params.get("auto_resize", False)
        wait_for_response = params.get("wait_for_response", True)
        operation = params.get("operation", "show_dialog")
        
        # Validate parameter types
        if not isinstance(title, str):
            return create_error_response(f"Parameter 'title' must be a string, got {type(title).__name__}.", with_readme=False)
        if not isinstance(width, int) or width <= 0:
            return create_error_response(f"Parameter 'width' must be a positive integer, got {width}.", with_readme=False)
        if not isinstance(height, int) or height <= 0:
            return create_error_response(f"Parameter 'height' must be a positive integer, got {height}.", with_readme=False)
        if not isinstance(modal, bool):
            return create_error_response(f"Parameter 'modal' must be a boolean, got {type(modal).__name__}.", with_readme=False)
        if not isinstance(timeout, int) or timeout < 0:
            return create_error_response(f"Parameter 'timeout' must be a non-negative integer, got {timeout}.", with_readme=False)
        if not isinstance(center_on_screen, bool):
            return create_error_response(f"Parameter 'center_on_screen' must be a boolean, got {type(center_on_screen).__name__}.", with_readme=False)
        if not isinstance(always_on_top, bool):
            return create_error_response(f"Parameter 'always_on_top' must be a boolean, got {type(always_on_top).__name__}.", with_readme=False)
        if not isinstance(bring_to_front, bool):
            return create_error_response(f"Parameter 'bring_to_front' must be a boolean, got {type(bring_to_front).__name__}.", with_readme=False)
        if not isinstance(auto_resize, bool):
            return create_error_response(f"Parameter 'auto_resize' must be a boolean, got {type(auto_resize).__name__}.", with_readme=False)
        if not isinstance(resizable, bool):
            return create_error_response(f"Parameter 'resizable' must be a boolean, got {type(resizable).__name__}.", with_readme=False)
        if not isinstance(wait_for_response, bool):
            return create_error_response(f"Parameter 'wait_for_response' must be a boolean, got {type(wait_for_response).__name__}.", with_readme=False)
        
        # Log the request
        content_type = "URL" if url else "HTML"
        content_preview = url if url else f"{len(html)} chars"
        wait_mode = "async" if not wait_for_response else "sync"
        MCPLogger.log(TOOL_LOG_NAME, f"Processing {operation} request ({wait_mode}): {content_type}={content_preview}, title='{title}', size={width}x{height}, modal={modal}, resizable={resizable}")
        
        # Try to show the window
        try:
            result = _show_webengine_window(html, title, width, height, modal, timeout, center_on_screen, always_on_top, bring_to_front, auto_resize, url, resizable, wait_for_response)
            return {
                "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                "isError": False
            }
        except ImportError as e:
            if "PySide" in str(e) or "Qt" in str(e):
                return create_error_response(
                    "Qt/PySide not available. This tool requires the friday.py Qt infrastructure to be running. "
                    "Please ensure the server was started via friday.py with Qt support.", 
                    with_readme=False
                )
            else:
                raise
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Exception in show_html_window: {str(e)}")
            MCPLogger.log(TOOL_LOG_NAME, f"Stack trace: {traceback.format_exc()}")
            return create_error_response(f"Error showing HTML window: {str(e)}", with_readme=False)
            
    except Exception as e:
        return create_error_response(f"Error processing HTML window request: {str(e)}", with_readme=True)


def _show_webengine_window(html: str, title: str, width: int, height: int, modal: bool, timeout: int, 
                          center_on_screen: bool = True, always_on_top: bool = True, bring_to_front: bool = True, 
                          auto_resize: bool = False, url: str = None, resizable: bool = False, 
                          wait_for_response: bool = True) -> Dict:
    """Internal function to show HTML popup via thread-safe queue message passing to friday.py.
    
    Uses Python's native queue.Queue for fast, reliable communication between MCP thread and Qt main thread.
    Follows the request-reply pattern from Thread_message_passing_in_Python.md
    
    Args:
        html: HTML content to display (mutually exclusive with url)
        title: Window title  
        width: Window width in pixels
        height: Window height in pixels
        modal: Whether dialog should be modal
        timeout: Timeout in seconds (0 = no timeout)
        center_on_screen: Whether to center window on screen
        always_on_top: Whether to keep window above others
        bring_to_front: Whether to force window to foreground
        auto_resize: Whether to automatically resize window to fit content
        url: URL to load (mutually exclusive with html)
        resizable: Whether the window should be user-resizable
        wait_for_response: If False, return immediately without waiting for window to close
        
    Returns:
        Dict with user response data (or immediate status if wait_for_response=False)
    """
    try:
        thread_id = threading.current_thread().ident
        MCPLogger.log(TOOL_LOG_NAME, f"Using native queue message passing for HTML popup: {title} - user.py v{USER_TOOL_VERSION} ({USER_TOOL_VERSION_DESC}) [Thread: {thread_id}]")
        
        # Access friday.py's UI queue via sys.modules registry (no imports needed!)
        try:
            request_queue = sys.modules.get('friday_ui_queue')
            
            if request_queue is None:
                return {"status": "error", "error": "No UI request queue available. Ensure friday.py is running with Qt support."}
                
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Could not access friday.py queue via sys.modules: {e}")
            MCPLogger.log(TOOL_LOG_NAME, f"Registry access error stack trace: {traceback.format_exc()}")
            return {"status": "error", "error": "Could not access friday.py UI queue. Ensure server was started via friday.py."}
        
        # Create a reply queue for this specific request
        reply_queue = queue.Queue()
        
        # Window positioning and behavior options are now passed as function parameters
        
        # Prepare request data
        request_data = {
            "html": html,
            "url": url,
            "title": title,
            "width": width,
            "height": height,
            "modal": modal,
            "timeout": timeout,
            "center_on_screen": center_on_screen,
            "always_on_top": always_on_top,
            "bring_to_front": bring_to_front,
            "auto_resize": auto_resize,
            "resizable": resizable
        }
        
        # Create UI request message
        ui_request = UIRequest(
            operation="show_popup",
            data=request_data,
            reply_queue=reply_queue
        )
        
        # Send request to friday.py's main thread via queue
        request_queue.put(ui_request)
        
        # If not waiting for response, return immediately
        if not wait_for_response:
            MCPLogger.log(TOOL_LOG_NAME, f"Async mode: Window opened, returning immediately without waiting")
            return {
                "status": "success",
                "message": "Window opened successfully (async mode - not waiting for user response)",
                "async": True
            }
        
        # Wait for response from reply queue
        max_wait_time = timeout + 5 if timeout > 0 else 65  # Add 5 second buffer or default to 65s
        
        try:
            response = reply_queue.get(timeout=max_wait_time)
            return response
            
        except queue.Empty:
            MCPLogger.log(TOOL_LOG_NAME, f"UI request timed out after {max_wait_time} seconds")
            return {"status": "timeout", "error": f"UI request timed out after {max_wait_time} seconds"}
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"CRITICAL ERROR in queue message passing: {str(e)}")
        MCPLogger.log(TOOL_LOG_NAME, f"Complete stack trace: {traceback.format_exc()}")
        return {"status": "error", "error": f"Error in UI communication: {str(e)}"}


def show_toast_notification(params: Dict) -> Dict:
    """Show a toast notification message in the system tray.
    
    Args:
        params: Dictionary containing the operation parameters including message and level
        
    Returns:
        Dict containing success status or error information
    """
    try:
        # Extract parameters
        message = params.get("message")
        level = params.get("level", "info")
        
        # Validate message parameter
        if not message:
            return create_error_response("Missing required parameter: 'message' is required for show_toast operation", with_readme=True)
        if not isinstance(message, str):
            return create_error_response(f"Parameter 'message' must be a string, got {type(message).__name__}", with_readme=False)
        
        # Validate level parameter
        valid_levels = ["info", "warning", "error", "success"]
        if level not in valid_levels:
            return create_error_response(f"Parameter 'level' must be one of {valid_levels}, got '{level}'", with_readme=False)
        
        MCPLogger.log(TOOL_LOG_NAME, f"Showing toast notification: [{level}] {message}")
        
        # Try to emit the toast message via friday.py's message queue
        try:
            result = _emit_toast_message(message, level)
            return {
                "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                "isError": False
            }
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Toast notification failed: {str(e)}")
            MCPLogger.log(TOOL_LOG_NAME, f"Stack trace: {traceback.format_exc()}")
            return create_error_response(f"Toast notification failed: {str(e)}", with_readme=False)
            
    except Exception as e:
        return create_error_response(f"Error processing toast notification: {str(e)}", with_readme=True)


def _emit_toast_message(message: str, level: str) -> Dict:
    """Internal function to emit a toast message to friday.py's message queue.
    
    Args:
        message: The toast message text
        level: Message level (info, warning, error, success)
        
    Returns:
        Dict with status information
    """
    try:
        thread_id = threading.current_thread().ident
        MCPLogger.log(TOOL_LOG_NAME, f"Emitting toast message: [{level}] {message} [Thread: {thread_id}]")
        
        # Access friday.py's engine instance via sys.modules
        try:
            friday_module = sys.modules.get('__main__')
            
            if friday_module is None:
                return {"status": "error", "error": "Could not access friday.py module. Ensure server was started via friday.py."}
            
            # Get the engine instance which has the _emit_message method
            engine = getattr(friday_module, 'engine', None)
            
            if engine is None:
                return {"status": "error", "error": "No engine instance available. Ensure friday.py is running with Qt support."}
            
            # Check if engine has the _emit_message method
            if not hasattr(engine, '_emit_message'):
                return {"status": "error", "error": "Engine does not have _emit_message method. Ensure friday.py version supports toast messages."}
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Could not access friday.py engine: {e}")
            MCPLogger.log(TOOL_LOG_NAME, f"Registry access error stack trace: {traceback.format_exc()}")
            return {"status": "error", "error": "Could not access friday.py engine. Ensure server was started via friday.py."}
        
        # Format message with level prefix for visual distinction
        level_prefixes = {
            "info": "ℹ️",
            "success": "✅",
            "warning": "⚠️",
            "error": "❌"
        }
        prefix = level_prefixes.get(level, "ℹ️")
        formatted_message = f"{prefix} {message}"
        
        # Emit the message using the engine's _emit_message method
        try:
            engine._emit_message(formatted_message, level)
            MCPLogger.log(TOOL_LOG_NAME, f"Toast message emitted successfully: [{level}] {message}")
            
            return {
                "status": "success",
                "message": "Toast notification sent successfully",
                "level": level,
                "text": message
            }
            
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Failed to emit message: {str(e)}")
            return {"status": "error", "error": f"Failed to emit toast message: {str(e)}"}
        
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"CRITICAL ERROR in toast emission: {str(e)}")
        MCPLogger.log(TOOL_LOG_NAME, f"Complete stack trace: {traceback.format_exc()}")
        return {"status": "error", "error": f"Error in toast communication: {str(e)}"}


def collect_api_key_from_user(params: Dict) -> Dict:
    """Collect an API key from the user using a specialized HTML dialog.
    
    Args:
        params: Dictionary containing the operation parameters including service_name and service_url
        
    Returns:
        Dict containing the user response with API key or error information
    """
    try:
        # Extract parameters
        service_name = params.get("service_name", "API Service")
        service_url = params.get("service_url", "")
        
        MCPLogger.log(TOOL_LOG_NAME, f"Collecting API key for {service_name}")
        
        # Generate HTML for API key collection
        html = _generate_api_key_collection_html(service_name, service_url)
        
        # Set up parameters for the HTML window
        window_params = {
            "html": html,
            "title": f"{service_name} API Key Required",
            "width": 550,
            "height": 450,
            "modal": True,
            "timeout": 300,  # 5 minutes timeout
            "center_on_screen": True,
            "always_on_top": True,
            "bring_to_front": True,
            "auto_resize": False,
            "operation": "show_dialog"
        }
        
        # Show the HTML window and collect the result
        return show_html_window(window_params)
        
    except Exception as e:
        return create_error_response(f"Error collecting API key: {str(e)}", with_readme=False)


def _generate_api_key_collection_html(service_name: str, service_url: str) -> str:
    """Generate HTML for API key collection dialog.
    
    Uses the web server's /api/settings endpoint to save the API key directly,
    eliminating the need for QWebChannel bridge communication.
    
    Args:
        service_name: Name of the service requiring the API key
        service_url: URL where users can obtain an API key
        
    Returns:
        str: Complete HTML for the API key collection dialog
    """
    # Get server configuration to construct API endpoint URL
    from ragtag.shared_config import get_config_manager
    config_manager = get_config_manager()
    server_config = config_manager.get_server_config()
    
    # Extract server details
    host = server_config.get("host", "127-0-0-1.local.aurafriday.com")
    port = server_config.get("port", 31173)
    enable_https = server_config.get("enable_https", True)
    protocol = "https" if enable_https else "http"
    
    # Get the ephemeral API key for authentication
    # This is the _internal user's API key that was generated at server startup
    import sys
    ephemeral_api_key = None
    try:
        # Access friday.py's EPHEMERAL_API_KEY global variable via sys.modules
        friday_module = sys.modules.get('__main__')
        if friday_module and hasattr(friday_module, 'EPHEMERAL_API_KEY'):
            ephemeral_api_key = friday_module.EPHEMERAL_API_KEY
            MCPLogger.log(TOOL_LOG_NAME, f"Retrieved ephemeral API key for authentication")
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"Warning: Could not retrieve ephemeral API key: {e}")
    
    # Construct the authenticated settings API endpoint URL
    # Format: {protocol}://{api_key}-{host}:{port}/api/settings/api_keys
    if ephemeral_api_key:
        api_url = f"{protocol}://{ephemeral_api_key}-{host}:{port}/api/settings/api_keys"
    else:
        # Fallback without authentication (will likely fail but better than crashing)
        api_url = f"{protocol}://{host}:{port}/api/settings/api_keys"
        MCPLogger.log(TOOL_LOG_NAME, f"Warning: Using unauthenticated API URL - request may fail")
    
    # Create the service URL link if provided
    service_link_html = ""
    if service_url:
        service_link_html = f"""
        <p style="margin: 15px 0; text-align: center;">
            <a href="{service_url}" target="_blank" style="color: #0066cc; text-decoration: none;">
                🔗 Get your {service_name} API key here
            </a>
        </p>"""
    
    # Determine the API key config key name (convert service_name to uppercase snake_case)
    api_key_name = service_name.upper().replace(' ', '_').replace('-', '_') + '_API_KEY'
    
    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>{service_name} API Key Required</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            padding: 30px;
            background: #f5f5f5;
            margin: 0;
            line-height: 1.6;
        }}
        .container {{
            background: white;
            padding: 30px;
            border-radius: 12px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.1);
            max-width: 450px;
            margin: 0 auto;
        }}
        h2 {{
            color: #333;
            margin-top: 0;
            margin-bottom: 10px;
            text-align: center;
        }}
        .service-info {{
            text-align: center;
            color: #666;
            margin-bottom: 25px;
            font-size: 14px;
        }}
        input[type="password"] {{
            width: 100%;
            padding: 12px;
            border: 2px solid #ddd;
            border-radius: 6px;
            font-size: 14px;
            margin: 10px 0;
            transition: border-color 0.3s;
            box-sizing: border-box;
        }}
        input[type="password"]:focus {{
            outline: none;
            border-color: #0066cc;
        }}
        .button-container {{
            display: flex;
            gap: 10px;
            margin-top: 25px;
            justify-content: center;
        }}
        button {{
            background: #0066cc;
            color: white;
            border: none;
            padding: 12px 24px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 500;
            transition: background-color 0.3s;
        }}
        button:hover {{
            background: #0052a3;
        }}
        button:disabled {{
            background: #999;
            cursor: not-allowed;
        }}
        .cancel {{
            background: #666;
        }}
        .cancel:hover {{
            background: #333;
        }}
        .instruction {{
            background: #e3f2fd;
            padding: 15px;
            border-radius: 6px;
            margin: 15px 0;
            font-size: 13px;
            color: #1565c0;
            border-left: 4px solid #0066cc;
        }}
        .error {{
            color: #d32f2f;
            font-size: 13px;
            margin-top: 5px;
            display: none;
        }}
        .success {{
            color: #2e7d32;
            font-size: 13px;
            margin-top: 5px;
            display: none;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h2>🔑 {service_name} API Key Required</h2>
        <div class="service-info">
            Please provide your {service_name} API key to continue
        </div>
        
        {service_link_html}
        
        <div class="instruction">
            💡 Your API key will be securely saved to your configuration file for future use.
        </div>
        
        <input type="password" id="apiKey" placeholder="Enter your API key..." autofocus>
        <div class="error" id="errorMessage"></div>
        <div class="success" id="successMessage"></div>
        
        <div class="button-container">
            <button id="submitBtn" onclick="submit()">Save & Continue</button>
            <button class="cancel" onclick="cancel()">Cancel</button>
        </div>
    </div>
    
    <script>
        // Use old JavaScript syntax compatible with PySide2's older WebEngine
        var apiUrl = '{api_url}';
        var serviceName = '{service_name}';
        var apiKeyName = '{api_key_name}';
        
        function showError(message) {{
            var errorEl = document.getElementById('errorMessage');
            var successEl = document.getElementById('successMessage');
            errorEl.textContent = message;
            errorEl.style.display = 'block';
            successEl.style.display = 'none';
        }}
        
        function showSuccess(message) {{
            var errorEl = document.getElementById('errorMessage');
            var successEl = document.getElementById('successMessage');
            successEl.textContent = message;
            successEl.style.display = 'block';
            errorEl.style.display = 'none';
        }}
        
        function submit() {{
            var key = document.getElementById('apiKey').value.trim();
            var errorEl = document.getElementById('errorMessage');
            var submitBtn = document.getElementById('submitBtn');
            
            // Clear previous errors
            errorEl.style.display = 'none';
            
            if (!key) {{
                showError('Please enter an API key');
                return;
            }}
            
            if (key.length < 10) {{
                showError('API key appears to be too short');
                return;
            }}
            
            // Disable button during submission
            submitBtn.disabled = true;
            submitBtn.textContent = 'Saving...';
            
            // Use GET then PUT approach: fetch existing keys, merge, then save
            // This preserves other API keys in the config
            console.log('Step 1: GET existing API keys from: ' + apiUrl);
            
            var xhrGet = new XMLHttpRequest();
            xhrGet.open('GET', apiUrl, true);
            xhrGet.withCredentials = true;
            
            xhrGet.onload = function() {{
                console.log('GET completed with status: ' + xhrGet.status);
                
                if (xhrGet.status === 200) {{
                    // Parse existing api_keys
                    var apiKeys = {{}};
                    try {{
                        apiKeys = JSON.parse(xhrGet.responseText) || {{}};
                        console.log('Parsed existing keys: ' + JSON.stringify(apiKeys));
                    }} catch (e) {{
                        console.log('Parse failed, starting with empty object: ' + e);
                        apiKeys = {{}};
                    }}
                    
                    // Add/update the new key
                    apiKeys[apiKeyName] = key;
                    console.log('Updated keys: ' + JSON.stringify(apiKeys));
                    
                    // Now PUT the updated api_keys object back
                    console.log('Step 2: PUT updated keys to: ' + apiUrl);
                    
                    var xhrPut = new XMLHttpRequest();
                    xhrPut.open('PUT', apiUrl, true);
                    xhrPut.withCredentials = true;
                    xhrPut.setRequestHeader('Content-Type', 'application/json');
                    
                    xhrPut.onload = function() {{
                        console.log('PUT completed with status: ' + xhrPut.status);
                        submitBtn.disabled = false;
                        submitBtn.textContent = 'Save & Continue';
                        
                        if (xhrPut.status === 200) {{
                            showSuccess('API key saved successfully!');
                            
                            // Set response BEFORE closing to ensure it's read by Python
                            window.userResponse = {{
                                "status": "success",
                                "service": serviceName
                            }};
                            
                            // Close window after delay to ensure response is captured
                            setTimeout(function() {{
                                window.close();
                            }}, 1000);
                        }} else {{
                            var errorMsg = 'Failed to save API key';
                            try {{
                                var errorData = JSON.parse(xhrPut.responseText);
                                if (errorData.error) {{
                                    errorMsg = errorData.error;
                                }}
                            }} catch (e) {{
                                console.log('Error parsing PUT response: ' + e);
                            }}
                            showError(errorMsg + ' (PUT Status: ' + xhrPut.status + ')');
                        }}
                    }};
                    
                    xhrPut.onerror = function() {{
                        console.log('PUT request failed - network error');
                        submitBtn.disabled = false;
                        submitBtn.textContent = 'Save & Continue';
                        showError('Network error during save (Status: ' + xhrPut.status + ')');
                    }};
                    
                    var payload = JSON.stringify(apiKeys);
                    console.log('Sending PUT payload: ' + payload);
                    xhrPut.send(payload);
                }} else {{
                    console.log('GET failed with status: ' + xhrGet.status);
                    submitBtn.disabled = false;
                    submitBtn.textContent = 'Save & Continue';
                    showError('Failed to fetch current API keys (GET Status: ' + xhrGet.status + ')');
                }}
            }};
            
            xhrGet.onerror = function() {{
                console.log('GET request failed - network error');
                submitBtn.disabled = false;
                submitBtn.textContent = 'Save & Continue';
                showError('Network error fetching keys (Status: ' + xhrGet.status + ')');
            }};
            
            xhrGet.send();
        }}
        
        function cancel() {{
            window.userResponse = {{
                "status": "cancelled",
                "message": "User cancelled API key entry"
            }};
            window.close();
        }}
        
        // Submit on Enter key
        document.getElementById('apiKey').addEventListener('keypress', function(e) {{
            if (e.key === 'Enter' || e.keyCode === 13) {{
                submit();
            }}
        }});
        
        // Focus the input field
        document.getElementById('apiKey').focus();
    </script>
</body>
</html>"""
    
    return html


def send_message_to_user(params: Dict) -> Dict:
    """
    Send async message to user without blocking.
    
    Parameters from params dict:
    - content: Message text (required)
    - msg_type: "question", "status", "notification", "response" (default: "status")
    - priority: "low", "normal", "high", "critical" (default: "normal")
    - requires_response: bool (default: False)
    - show_dashboard: bool (default: True)
    """
    try:
        content = params.get("content")
        if not content:
            return create_error_response("Missing required parameter: content", with_readme=False)
        
        msg_type = params.get("msg_type", "status")
        priority = params.get("priority", "normal")
        requires_response = params.get("requires_response", False)
        show_dashboard = params.get("show_dashboard", True)
        
        # Create message object
        message = {
            'id': str(uuid.uuid4()),
            'timestamp': time.time(),
            'direction': 'ai_to_user',
            'type': msg_type,
            'priority': priority,
            'content': content,
            'requires_response': requires_response,
            'status': 'pending'
        }
        
        MCPLogger.log(TOOL_LOG_NAME, f"Sending message to user: [{msg_type}/{priority}] {content[:50]}...")
        
        # Send via queue to friday.py
        try:
            request_queue = sys.modules.get('friday_ui_queue')
            if request_queue is None:
                return create_error_response("No UI request queue available. Ensure friday.py is running.", with_readme=False)
            
            reply_queue = queue.Queue()
            
            request_data = {
                'operation': 'send_message',
                'message': message,
                'show_dashboard': show_dashboard
            }
            
            ui_request = UIRequest(
                operation="send_message",
                data=request_data,
                reply_queue=reply_queue
            )
            
            request_queue.put(ui_request)
            
            # Wait for confirmation
            try:
                response = reply_queue.get(timeout=5)
                
                return {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "message_id": message['id'],
                            "status": "queued",
                            "timestamp": message['timestamp']
                        }, indent=2)
                    }],
                    "isError": False
                }
            except queue.Empty:
                return create_error_response("Timeout waiting for message queue confirmation", with_readme=False)
                
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Error sending message: {str(e)}")
            return create_error_response(f"Error sending message: {str(e)}", with_readme=False)
            
    except Exception as e:
        return create_error_response(f"Error in send_message: {str(e)}", with_readme=False)


def check_user_messages(params: Dict) -> Dict:
    """
    Check for messages from user (non-blocking).
    
    Parameters from params dict:
    - mark_as_read: bool (default: True)
    - filter_type: Optional filter by message type
    - since_timestamp: Only get messages after this time
    """
    try:
        mark_as_read = params.get("mark_as_read", True)
        filter_type = params.get("filter_type")
        since_timestamp = params.get("since_timestamp")
        
        MCPLogger.log(TOOL_LOG_NAME, f"Checking for user messages (filter_type={filter_type}, since={since_timestamp})")
        
        # Get messages from friday.py queue
        try:
            request_queue = sys.modules.get('friday_ui_queue')
            if request_queue is None:
                return create_error_response("No UI request queue available. Ensure friday.py is running.", with_readme=False)
            
            reply_queue = queue.Queue()
            
            request_data = {
                'operation': 'check_messages',
                'mark_as_read': mark_as_read,
                'filter_type': filter_type,
                'since_timestamp': since_timestamp
            }
            
            ui_request = UIRequest(
                operation="check_messages",
                data=request_data,
                reply_queue=reply_queue
            )
            
            request_queue.put(ui_request)
            
            # Wait for response
            try:
                response = reply_queue.get(timeout=5)
                messages = response.get('messages', [])
                
                MCPLogger.log(TOOL_LOG_NAME, f"Retrieved {len(messages)} messages from user")
                
                return {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "messages": messages,
                            "count": len(messages)
                        }, indent=2)
                    }],
                    "isError": False
                }
            except queue.Empty:
                return create_error_response("Timeout waiting for message check response", with_readme=False)
                
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Error checking messages: {str(e)}")
            return create_error_response(f"Error checking messages: {str(e)}", with_readme=False)
            
    except Exception as e:
        return create_error_response(f"Error in check_messages: {str(e)}", with_readme=False)


def show_message_dashboard(params: Dict) -> Dict:
    """Show the persistent message dashboard window."""
    try:
        MCPLogger.log(TOOL_LOG_NAME, "Showing message dashboard")
        
        try:
            request_queue = sys.modules.get('friday_ui_queue')
            if request_queue is None:
                return create_error_response("No UI request queue available. Ensure friday.py is running.", with_readme=False)
            
            reply_queue = queue.Queue()
            
            request_data = {
                'operation': 'show_dashboard'
            }
            
            ui_request = UIRequest(
                operation="show_dashboard",
                data=request_data,
                reply_queue=reply_queue
            )
            
            request_queue.put(ui_request)
            
            try:
                response = reply_queue.get(timeout=10)
                
                return {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "status": "success",
                            "message": "Dashboard shown"
                        }, indent=2)
                    }],
                    "isError": False
                }
            except queue.Empty:
                return create_error_response("Timeout waiting for dashboard to show", with_readme=False)
                
        except Exception as e:
            MCPLogger.log(TOOL_LOG_NAME, f"Error showing dashboard: {str(e)}")
            return create_error_response(f"Error showing dashboard: {str(e)}", with_readme=False)
            
    except Exception as e:
        return create_error_response(f"Error in show_dashboard: {str(e)}", with_readme=False)


def hide_message_dashboard(params: Dict) -> Dict:
    """Hide the message dashboard window."""
    try:
        MCPLogger.log(TOOL_LOG_NAME, "Hiding message dashboard")
        
        try:
            request_queue = sys.modules.get('friday_ui_queue')
            if request_queue is None:
                return create_error_response("No UI request queue available.", with_readme=False)
            
            reply_queue = queue.Queue()
            
            ui_request = UIRequest(
                operation="hide_dashboard",
                data={'operation': 'hide_dashboard'},
                reply_queue=reply_queue
            )
            
            request_queue.put(ui_request)
            
            try:
                response = reply_queue.get(timeout=5)
                return {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({"status": "success", "message": "Dashboard hidden"}, indent=2)
                    }],
                    "isError": False
                }
            except queue.Empty:
                return create_error_response("Timeout waiting for dashboard to hide", with_readme=False)
                
        except Exception as e:
            return create_error_response(f"Error hiding dashboard: {str(e)}", with_readme=False)
            
    except Exception as e:
        return create_error_response(f"Error in hide_dashboard: {str(e)}", with_readme=False)


def get_message_history(params: Dict) -> Dict:
    """Get all message history."""
    try:
        MCPLogger.log(TOOL_LOG_NAME, "Getting message history")
        
        try:
            request_queue = sys.modules.get('friday_ui_queue')
            if request_queue is None:
                return create_error_response("No UI request queue available.", with_readme=False)
            
            reply_queue = queue.Queue()
            
            ui_request = UIRequest(
                operation="get_message_history",
                data={'operation': 'get_message_history'},
                reply_queue=reply_queue
            )
            
            request_queue.put(ui_request)
            
            try:
                response = reply_queue.get(timeout=5)
                history = response.get('history', [])
                
                return {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "history": history,
                            "count": len(history)
                        }, indent=2)
                    }],
                    "isError": False
                }
            except queue.Empty:
                return create_error_response("Timeout waiting for message history", with_readme=False)
                
        except Exception as e:
            return create_error_response(f"Error getting message history: {str(e)}", with_readme=False)
            
    except Exception as e:
        return create_error_response(f"Error in get_message_history: {str(e)}", with_readme=False)


def clear_message_queues(params: Dict) -> Dict:
    """Clear all message queues."""
    try:
        MCPLogger.log(TOOL_LOG_NAME, "Clearing message queues")
        
        try:
            request_queue = sys.modules.get('friday_ui_queue')
            if request_queue is None:
                return create_error_response("No UI request queue available.", with_readme=False)
            
            reply_queue = queue.Queue()
            
            ui_request = UIRequest(
                operation="clear_messages",
                data={'operation': 'clear_messages'},
                reply_queue=reply_queue
            )
            
            request_queue.put(ui_request)
            
            try:
                response = reply_queue.get(timeout=5)
                
                return {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "status": "success",
                            "message": "Message queues cleared"
                        }, indent=2)
                    }],
                    "isError": False
                }
            except queue.Empty:
                return create_error_response("Timeout waiting for clear confirmation", with_readme=False)
                
        except Exception as e:
            return create_error_response(f"Error clearing messages: {str(e)}", with_readme=False)
            
    except Exception as e:
        return create_error_response(f"Error in clear_messages: {str(e)}", with_readme=False)


def handle_user(input_param: Dict) -> Dict:
    """Handle user interaction tool operations via MCP interface."""
    try:
        
        # Pop off synthetic handler_info parameter early (before validation)
        # This is added by the server for tools that need dynamic routing
        handler_info = input_param.pop('handler_info', None)
        
        if isinstance(input_param, dict) and "input" in input_param: # collapse the single-input placeholder which exists only to save context (because we must bypass pipeline parameter validation to *save* the context)
            input_param = input_param["input"]

        # Handle readme operation first (before token validation)
        if isinstance(input_param, dict) and input_param.get("operation") == "readme":
            return {
                "content": [{"type": "text", "text": readme(True)}],
                "isError": False
            }
            
        # Validate input structure first
        if not isinstance(input_param, dict):
            return create_error_response("Invalid input format. Expected dictionary with tool parameters.", with_readme=True)
            
        # Check for token - if missing or invalid, return readme
        provided_token = input_param.get("tool_unlock_token")
        
        # Check for inter-tool token (starts with "-")
        is_inter_tool_call = False
        if provided_token and provided_token.startswith("-"):
            # Parse inter-tool token: "-{calling_tool_token}-{target_tool_token}"
            try:
                parts = provided_token[1:].split("-", 1)  # Remove leading "-" and split once
                if len(parts) == 2:
                    calling_tool_token, target_tool_token = parts
                    if target_tool_token == TOOL_UNLOCK_TOKEN:
                        is_inter_tool_call = True
                        MCPLogger.log(TOOL_LOG_NAME, f"Inter-tool call detected from tool with token: {calling_tool_token[:8]}...")
                    else:
                        MCPLogger.log(TOOL_LOG_NAME, f"Inter-tool call attempted but target token mismatch")
                else:
                    MCPLogger.log(TOOL_LOG_NAME, f"Malformed inter-tool token: {provided_token[:20]}...")
            except Exception as e:
                MCPLogger.log(TOOL_LOG_NAME, f"Error parsing inter-tool token: {e}")
        
        # Validate token (either exact match or valid inter-tool call)
        if provided_token != TOOL_UNLOCK_TOKEN and not is_inter_tool_call:
            return create_error_response("Invalid or missing tool_unlock_token: this indicates your context is missing the following details, which are needed to correctly use this tool:", with_readme=True )

        # Validate all parameters using schema
        error_msg, validated_params = validate_parameters(input_param)
        if error_msg:
            return create_error_response(error_msg, with_readme=True)

        # Extract validated parameters
        operation = validated_params.get("operation")
        
        # Handle operations
        if operation == "test_queue":
            # Test queue communication without Qt operations
            result = test_queue_communication(validated_params)
            return result
        elif operation == "show_toast":
            # Show toast notification message
            result = show_toast_notification(validated_params)
            return result
        elif operation == "collect_api_key":
            # Collect API key from user using a specialized dialog
            result = collect_api_key_from_user(validated_params)
            return result
        elif operation in ["show_popup", "show_dialog"]:
            # Both operations use the same handler, with modal determined by the operation or explicit parameter
            if operation == "show_popup":
                validated_params["modal"] = False  # Force non-modal for show_popup
            elif operation == "show_dialog" and "modal" not in validated_params:
                validated_params["modal"] = True   # Default to modal for show_dialog
                
            result = show_html_window(validated_params)
            return result
        elif operation == "send_message":
            # Send async message to user without blocking
            result = send_message_to_user(validated_params)
            return result
        elif operation == "check_messages":
            # Check for messages from user
            result = check_user_messages(validated_params)
            return result
        elif operation == "show_dashboard":
            # Show persistent message dashboard
            result = show_message_dashboard(validated_params)
            return result
        elif operation == "hide_dashboard":
            # Hide the message dashboard
            result = hide_message_dashboard(validated_params)
            return result
        elif operation == "get_message_history":
            # Get all message history
            result = get_message_history(validated_params)
            return result
        elif operation == "clear_messages":
            # Clear message queues
            result = clear_message_queues(validated_params)
            return result
        elif operation == "readme":
            # This should have been handled above, but just in case
            return {
                "content": [{"type": "text", "text": readme(True)}],
                "isError": False
            }
        else:
            # Get valid operations from the schema enum
            valid_operations = TOOLS[0]["real_parameters"]["properties"]["operation"]["enum"]
            return create_error_response(f"Unknown operation: '{operation}'. Available operations: {', '.join(valid_operations)}", with_readme=True)
            
    except Exception as e:
        MCPLogger.log(TOOL_LOG_NAME, f"CRITICAL ERROR in handle_user: {str(e)}")
        MCPLogger.log(TOOL_LOG_NAME, f"Complete stack trace: {traceback.format_exc()}")
        return create_error_response(f"Error in user interaction operation: {str(e)}", with_readme=True)

# Map of tool names to their handlers
HANDLERS = {
    "user": handle_user
}
