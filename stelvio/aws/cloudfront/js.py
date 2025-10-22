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
    return f"""
        function handler(event) {{
            var request = event.request;
            var uri = request.uri;
            // Strip the path prefix '{path_pattern}'
            if (uri.startsWith('{path_pattern}/')) {{
                request.uri = uri.substring({len(path_pattern)});
            }}
            return request;
        }}
        """.strip()