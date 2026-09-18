import React from "react";
import { createRoot } from "react-dom/client";
// Fonts are bundled rather than fetched from a CDN: the demo has to work
// offline and inside a container with no egress, and a self-hosted variable
// font also removes the layout shift a late-arriving webfont causes.
import "@fontsource-variable/inter";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/500.css";
import App from "./App.jsx";
import "./styles.css";

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
