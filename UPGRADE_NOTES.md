# Munim deployment changes

- Replaced both discontinued Groq model defaults and added a no-send readiness check.
- Added support for Supabase backend secret keys and explicit table privileges for the Data API.
- Added persistent digest delivery status. The bot claims each daily slot before calling Telegram and records a successful delivery. A rejected send can be retried. An ambiguous send requires checking Telegram first.
- Kept dated market observations, ordered headlines by publication time, and attached URLs from the fetched sources. The prompt asks for competing explanations and what would distinguish them.
- Prevented repeat notes and previously seen article URLs from increasing confidence in normal recent runs. Invalid prediction dates are skipped. Prediction grading remains independent.
- Changed the workflow to two daily reports including weekends. The morning job moved from 08:30 to 08:35 IST to avoid GitHub's top-of-hour queue.

For migration and hosting steps, see [README.md](README.md).
