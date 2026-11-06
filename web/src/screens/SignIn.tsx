import { loginUrl } from "../api";

export default function SignIn() {
  const next = window.location.pathname + window.location.search;
  return (
    <section className="card center">
      <h1>Cashbook admin</h1>
      <p className="muted">Sign in with the owner's Google account.</p>
      <a className="button primary" href={loginUrl(next)}>Continue with Google</a>
    </section>
  );
}
