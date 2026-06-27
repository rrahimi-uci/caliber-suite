/**
 * Toast notification helpers.
 *
 * Wraps sonner's `toast` API so the rest of the app has a single import
 * for success/error/info toasts. The `<Toaster>` component is mounted
 * once in `main.tsx`.
 */

import { toast } from "sonner";

export { toast };

export const showToast = {
  success(message: string): void {
    toast.success(message);
  },
  error(message: string): void {
    toast.error(message);
  },
  info(message: string): void {
    toast.info(message);
  },
  warning(message: string): void {
    toast.warning(message);
  },
};
