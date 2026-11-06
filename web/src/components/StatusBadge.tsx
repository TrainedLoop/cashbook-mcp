import type { Connection } from "../api";

const ICON: Record<Connection["badge"], string> = {
  ok: "✓",
  syncing: "↻",
  outdated: "!",
  "login error": "✕",
  "consent revoked": "✕",
};

export default function StatusBadge({ badge }: { badge: Connection["badge"] }) {
  const tone = badge === "ok" ? "ok" : badge === "syncing" ? "info" : badge === "outdated" ? "warn" : "bad";
  return (
    <span className={`badge badge-${tone}`}>
      <span aria-hidden="true">{ICON[badge]}</span> {badge}
    </span>
  );
}
