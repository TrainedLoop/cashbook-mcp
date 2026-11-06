import { useEffect, useState } from "react";
import { api, type ServerInfo } from "../api";

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatTime(value: string | null): string {
  if (!value) return "never";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}

export default function ServerFooter() {
  const [info, setInfo] = useState<ServerInfo | null>(null);
  useEffect(() => {
    let live = true;
    api.server().then(
      (s) => live && setInfo(s),
      () => undefined,
    );
    return () => {
      live = false;
    };
  }, []);
  if (!info) return null;
  return (
    <footer className="footer">
      <dl>
        <div><dt>Last full refresh</dt><dd>{formatTime(info.last_full_refresh)}</dd></div>
        <div><dt>Next refresh</dt><dd>{formatTime(info.next_full_refresh)}</dd></div>
        <div><dt>Database size</dt><dd>{formatBytes(info.db_size_bytes)}</dd></div>
        <div><dt>Schema version</dt><dd>{info.schema_version}</dd></div>
        <div><dt>App version</dt><dd>{info.app_version}</dd></div>
      </dl>
    </footer>
  );
}
