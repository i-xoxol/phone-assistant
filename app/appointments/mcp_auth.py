"""Incremental OAuth consent for appointment tools, preserving the existing client."""
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.types import CallToolResult, TextContent

SCOPES = ['phone.appointments.read', 'phone.appointments.book', 'phone.appointments.manage']
TOOL_SCOPES = {
    'get_appointment_permissions': SCOPES,
    'check_appointment_slot': [SCOPES[0]],
    'get_booking_status': [SCOPES[0]],
    'get_my_appointment': [SCOPES[0]],
    'report_call_request_progress': [SCOPES[0]],
    'book_appointment': [SCOPES[1]],
    'set_caller_relationship': [SCOPES[2]],
    'set_private_call_notice': [SCOPES[2]],
}


def security_schemes(name):
    return [{'type': 'oauth2', 'scopes': ['phone.calls', *TOOL_SCOPES[name]]}]


def authorization_metadata(name):
    return {'schemaVersion': '1.0', 'securitySchemes': security_schemes(name)}


def appointment_authorization(origin):
    """Expose both descriptor forms and the ChatGPT OAuth challenge on the wire.

    The SDK's Tool model does not yet retain top-level securitySchemes. Its
    middleware supports extending serialized results without changing validation
    or the existing event extension. Tool handlers still enforce scopes themselves.
    """
    metadata_url = origin.rstrip('/') + '/.well-known/oauth-protected-resource/mcp'

    async def authorize(ctx, call_next):
        if ctx.method == 'tools/call':
            name = (ctx.params or {}).get('name')
            if isinstance(name, str) and name in TOOL_SCOPES:
                required = ['phone.calls', *TOOL_SCOPES[name]]
                token = get_access_token()
                if token is None or not set(required).issubset(token.scopes):
                    # Request the existing grants too: reauthorization must not
                    # discard already approved appointment capabilities.
                    scopes = list(dict.fromkeys([
                        'phone.calls', *(s for s in (token.scopes if token else []) if s in SCOPES),
                        *TOOL_SCOPES[name],
                    ]))
                    challenge = (f'Bearer resource_metadata="{metadata_url}", '
                        'error="insufficient_scope", '
                        'error_description="Approve the additional Phone Assistant appointment permissions", '
                        f'scope="{" ".join(scopes)}"')
                    return CallToolResult(is_error=True, content=[TextContent(type='text', text=
                        'Additional appointment permission is required. Use the Connect/Authorize action '
                        'shown by your client and enter a fresh service-host MCP pairing code. '
                        'No appointment action has run. Keep the existing connection and event subscriptions.')],
                        _meta={'mcp/www_authenticate': [challenge]})
        result = await call_next(ctx)
        if ctx.method == 'tools/list':
            result = result.model_dump(by_alias=True, exclude_none=True) if hasattr(result, 'model_dump') else dict(result)
            result['tools'] = [{**tool, 'securitySchemes': security_schemes(tool['name'])}
                if tool['name'] in TOOL_SCOPES else tool for tool in result['tools']]
        return result

    return authorize
