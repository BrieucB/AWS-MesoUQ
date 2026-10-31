// Only the exact public / and /index.html GET/HEAD behaviors use this hook.
// It never inspects credentials, Host, cookies or query strings.
function handler(event) {
    return {
        statusCode: 302,
        statusDescription: "Found",
        headers: {
            location: {value: "/auth/login"},
            "cache-control": {value: "no-store"}
        }
    };
}
