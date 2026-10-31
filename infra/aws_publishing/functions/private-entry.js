// Rewriting the URI retains the selected signed behavior and S3 origin.
function handler(event) {
    var request = event.request;
    if ((request.method === "GET" || request.method === "HEAD") && request.uri === "/app") {
        request.uri = "/index.html";
        return request;
    }
    // Compatibility hint only; native signed-cookie enforcement is authoritative.
    if ((request.method === "GET" || request.method === "HEAD") &&
        (request.uri === "/" || request.uri === "/index.html")) {
        var cookies = request.cookies || {};
        var names = ["CloudFront-Policy", "CloudFront-Signature", "CloudFront-Key-Pair-Id"];
        for (var i = 0; i < names.length; i++) {
            if (!cookies[names[i]] || !cookies[names[i]].value) {
                return {
                    statusCode: 302,
                    statusDescription: "Found",
                    headers: {
                        location: {value: "/auth/login"},
                        "cache-control": {value: "no-store"}
                    }
                };
            }
        }
    }
    return request;
}
