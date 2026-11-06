# Roadmap

Planned and proposed work. Items move to the changelog (git history) when they ship.

## Proposed

- **OFX/CSV import provider.** For accounts that Open Finance does not cover, if a real need appears. This is the point to introduce a provider interface; today the code talks to Pluggy only.
- **Optional history carry-over on reconnect.** Reconnecting an alias to a new Pluggy item currently starts empty with whatever Pluggy provides. An opt-in carry-over would keep the accumulated history under the same alias.
