# Munim deployment changes

- Replaced both discontinued Groq model defaults and added a no-send readiness check.
- Added support for Supabase backend secret keys and explicit table privileges for the Data API.
- Added persistent digest delivery status. The bot claims each daily slot before calling Telegram and records a successful delivery. A rejected send can be retried. An ambiguous send requires checking Telegram first.
- Kept dated market observations, ordered headlines by publication time, and attached URLs from the fetched sources. The prompt asks for competing explanations and what would distinguish them.
- Prevented repeat notes and previously seen article URLs from increasing confidence in normal recent runs. Invalid prediction dates are skipped. Prediction grading remains independent.
- The workflow sends a morning market update at 10:15 IST and an evening wrap at 18:15 IST, including weekends. The morning briefing uses timestamped intraday bars from the Indian session when available.

For migration and hosting steps, see [README.md](README.md).
