import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

function App() {
  return (
    <>
      <header className="site-header">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true" />
          SignalWatch
        </div>
        <nav aria-label="Resources">
          <a href="/docs">
            API docs <span aria-hidden="true">↗</span>
          </a>
        </nav>
      </header>
      <main>
        <p className="eyebrow">Hello, and welcome</p>
        <h1>
          Welcome to <span>SignalWatch.</span>
        </h1>
        <p className="intro">
          A place for the updates that matter.
          <br />
          We’re just getting started.
        </p>
      </main>
    </>
  );
}

const root = document.getElementById("root");

if (!root) {
  throw new Error("The page is missing its root element.");
}

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
