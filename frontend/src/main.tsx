import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Root } from "./Root";
import "./index.css";

const root = document.getElementById("root");
if (!root) throw new Error("#root missing from index.html");

createRoot(root).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
