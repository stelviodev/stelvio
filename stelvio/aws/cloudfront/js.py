def default_404_function_js() -> str:
    return """
        function handler(event) {
            return {
                statusCode: 404,
                statusDescription: 'Not Found',
                headers: {
                    'content-type': { value: 'text/html' }
                },
                body: '<!DOCTYPE html><html><head><title>404 Not Found</title></head>'
                '<body><h1>404 Not Found</h1><p>The requested resource was not found.</p></body>'
                '</html>'
            };
        }
        """.strip()


def strip_path_pattern_function_js(path_pattern: str) -> str:
    path_len = len(path_pattern)
    return f"""
        function handler(event) {{
            var request = event.request;
            var uri = request.uri;
            if (uri === '{path_pattern}') {{
                request.uri = '/';
            }} else if (uri.substr(0, {path_len + 1}) === '{path_pattern}/') {{
                request.uri = uri.substr({path_len});
            }}
            return request;
        }}
        """.strip()


def set_custom_host_header(host: str) -> str:
    """Generate Lambda@Edge code to set a custom Host header for URL origins.

    This is necessary when proxying to external URLs that use virtual host routing.
    """
    # https://serverfault.com/a/888776
    return """
        'use strict';

        // force a specific Host header to be sent to the origin

        exports.handler = (event, context, callback) => {
            const request = event.Records[0].cf.request;
            request.headers.host[0].value = '{host}';
            return callback(null, request);
        };
        """.replace("{host}", host).strip()
