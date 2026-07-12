import { useEffect } from "react";
import { X, CheckCircle, AlertCircle, Info } from "lucide-react";
import { ToastNotification, useAppStore } from "../lib/store";

export default function Toasts() {
  const toasts = useAppStore((state) => state.toasts);
  const removeToast = useAppStore((state) => state.removeToast);

  return (
    <div className="pointer-events-none fixed bottom-3 left-3 right-3 z-[999999] flex flex-col gap-2 sm:bottom-4 sm:left-auto sm:right-4">
      {toasts.map((toast) => (
        <ToastItem key={toast.id} toast={toast} removeToast={removeToast} />
      ))}
    </div>
  );
}

function ToastItem({
  toast,
  removeToast,
}: {
  toast: ToastNotification;
  removeToast: (id: string) => void;
}) {
  const hasAction = Boolean(toast.action);

  useEffect(() => {
    const timer = window.setTimeout(
      () => removeToast(toast.id),
      toast.duration ?? (hasAction ? 6500 : 4000),
    );

    return () => window.clearTimeout(timer);
  }, [hasAction, removeToast, toast.duration, toast.id]);

  const handleAction = () => {
    const result = toast.action?.onClick();
    if (result instanceof Promise) {
      result.catch(() => undefined);
    }
    removeToast(toast.id);
  };

  const icon =
    toast.type === "success" ? (
      <CheckCircle className="h-5 w-5 shrink-0 text-green-400" />
    ) : toast.type === "error" ? (
      <AlertCircle className="h-5 w-5 shrink-0 text-red-400" />
    ) : (
      <Info className="h-5 w-5 shrink-0 text-blue-400" />
    );

  const colorClass =
    toast.type === "success"
      ? "border-green-500/20 bg-dark-900/90 shadow-green-900/10"
      : toast.type === "error"
        ? "border-red-500/20 bg-dark-900/90 shadow-red-900/10"
        : "border-blue-500/20 bg-dark-900/90 shadow-blue-900/10";

  return (
    <div
      className={`pointer-events-auto flex w-full min-w-0 items-center gap-3 rounded-xl border py-3 pl-4 pr-3 shadow-xl backdrop-blur-md animate-slide-up sm:min-w-[300px] sm:max-w-md ${colorClass}`}
    >
      {icon}
      <p className="min-w-0 flex-1 break-words text-sm font-medium text-white">
        {toast.message}
      </p>
      {toast.action && (
        <button
          onClick={handleAction}
          className="shrink-0 rounded-lg border border-primary-500/25 bg-primary-500/10 px-3 py-1.5 text-xs font-bold text-primary-300 transition-colors hover:bg-primary-500/20 hover:text-primary-200"
        >
          {toast.action.label}
        </button>
      )}
      <button
        onClick={() => removeToast(toast.id)}
        className="shrink-0 rounded-lg p-1 text-dark-400 transition-colors hover:bg-white/10 hover:text-white"
        aria-label="Dismiss notification"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}
