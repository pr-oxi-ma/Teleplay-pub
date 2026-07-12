/**
 * DeleteConfirmModal - confirmation dialog for deleting files/folders
 */
import { X, Trash2 } from "lucide-react";

interface DeleteConfirmModalProps {
  type: "file" | "folder" | "multiple";
  name?: string;
  count?: number;
  onConfirm: () => void;
  onClose: () => void;
  recycleBinEnabled?: boolean;
}

export default function DeleteConfirmModal({
  type,
  name,
  count = 1,
  onConfirm,
  onClose,
  recycleBinEnabled = true,
}: DeleteConfirmModalProps) {
  const title = count > 1 ? `Delete ${count} items` : `Delete ${type}`;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center overflow-x-hidden bg-black/50 p-4 backdrop-blur-sm">
      <div className="glass-card w-full min-w-0 max-w-sm overflow-hidden p-5 animate-slide-up sm:p-6">
        <div className="mb-4 flex min-w-0 items-center justify-between gap-3">
          <h2 className="flex min-w-0 items-center gap-2 truncate text-lg font-semibold text-red-400">
            <Trash2 className="h-5 w-5 shrink-0" />
            <span className="truncate">{title}</span>
          </h2>
          <button
            onClick={onClose}
            className="shrink-0 rounded p-1 hover:bg-dark-700"
            aria-label="Close delete confirmation"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="mb-6 min-w-0 text-dark-300">
          {count > 1 ? (
            <p>Are you sure you want to delete these {count} items?</p>
          ) : (
            <>
              <p>Are you sure you want to delete this {type}?</p>
              {name && (
                <p
                  className="mt-2 block w-full truncate rounded-lg border border-white/[0.05] bg-dark-800/50 px-3 py-2 font-medium text-white"
                  title={name}
                >
                  {name}
                </p>
              )}
            </>
          )}

          {type === "folder" && recycleBinEnabled && (
            <p className="mt-2 text-sm text-dark-400">
              The folder hierarchy and its files will stay together.
            </p>
          )}
          <p
            className={`mt-2 text-sm ${recycleBinEnabled ? "text-primary-300" : "text-red-400"}`}
          >
            {recycleBinEnabled
              ? "You can restore it from Recycle Bin before it expires."
              : "Recycle Bin is disabled. This action cannot be undone."}
          </p>
        </div>

        <div className="flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
          <button
            onClick={onClose}
            className="rounded-lg px-4 py-2 text-dark-400 transition-colors hover:bg-white/[0.04] hover:text-white"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            className="rounded-lg bg-red-600 px-4 py-2 font-medium transition-colors hover:bg-red-700"
          >
            {recycleBinEnabled ? "Move to Recycle Bin" : "Delete forever"}
          </button>
        </div>
      </div>
    </div>
  );
}
