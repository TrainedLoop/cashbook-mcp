"""Security headers on every /admin response (mailhub's set, with a CSP for the SPA)."""

# The Pluggy origins allow the Connect widget: its script and styles, its API calls and its iframe.
# verify on real widget (Task 17)
CSP = (
    "default-src 'none'; "
    "script-src 'self' https://cdn.pluggy.ai; "  # verify on real widget (Task 17)
    "style-src 'self' 'unsafe-inline' https://cdn.pluggy.ai; "  # verify on real widget (Task 17)
    "img-src 'self' data: https:; "
    "connect-src 'self' https://api.pluggy.ai; "  # verify on real widget (Task 17)
    "frame-src https://connect.pluggy.ai; "  # verify on real widget (Task 17)
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",  # not no-referrer: browsers then send "Origin: null" on POSTs
    # The default; the SPA routes set their own (no-cache for index.html, immutable for assets).
    "Cache-Control": "no-store",
}
