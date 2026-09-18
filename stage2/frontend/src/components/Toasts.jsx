import React, { useEffect } from "react";
import Icon from "./Icon.jsx";

/**
 * Transient confirmations for actions the operator took.
 *
 * Only for *outcomes of deliberate actions* — a ticket raised, a host
 * acknowledged, an export started. Incoming alerts never toast: they already
 * have the map, the banner, the list and the live region, and a toast for every
 * alert would be the thing that makes an operator turn notifications off.
 */
export default function Toasts({ toasts, onDismiss }) {
  return (
    <div className="toast-stack" aria-live="polite" aria-atomic="false">
      {toasts.map((toast) => (
        <Toast key={toast.id} toast={toast} onDismiss={onDismiss} />
      ))}
    </div>
  );
}

function Toast({ toast, onDismiss }) {
  useEffect(() => {
    const id = setTimeout(() => onDismiss(toast.id), toast.duration ?? 4200);
    return () => clearTimeout(id);
  }, [toast, onDismiss]);

  const icon = toast.kind === "error" ? "alert" : toast.kind === "success" ? "check" : "activity";
  return (
    <div className={`toast is-${toast.kind || "info"}`} data-testid="toast">
      <Icon name={icon} size={15} />
      <span>
        <span className="toast-title">{toast.title}</span>
        {toast.body && <span className="toast-body"> — {toast.body}</span>}
      </span>
    </div>
  );
}
