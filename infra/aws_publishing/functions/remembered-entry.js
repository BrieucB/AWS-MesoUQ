function handler(event) {
    var request = event.request;
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
