/**
 * Aria assistant logo. The image lives in ``public/`` and is served under the
 * app's static prefix (same convention as the CALIBER brand mark in TopBar).
 *
 * Pass ``alt=""`` when the logo sits next to a visible "Aria"/"Ask Aria" label
 * so it stays decorative and doesn't double up the element's accessible name.
 */

// Bump when the public/aria_caliber_assistant.png artwork is replaced so
// browsers don't serve a stale cached copy.
const ARIA_LOGO_VERSION = "2";

/** Resolved URL for the Aria avatar, honouring the deployment's static prefix. */
export function ariaLogoSrc(): string {
  const staticPrefix =
    (typeof window !== "undefined" && window.__CALIBER_STATIC_PREFIX__) || "";
  return `${staticPrefix}/caliber/aria_caliber_assistant.png?v=${ARIA_LOGO_VERSION}`;
}

type AriaLogoProps = Omit<JSX.IntrinsicElements["img"], "src"> & {
  className?: string;
  alt?: string;
};

export function AriaLogo({
  className,
  alt = "Aria",
  ...rest
}: AriaLogoProps): JSX.Element {
  return (
    <img
      src={ariaLogoSrc()}
      alt={alt}
      aria-hidden={alt === "" ? "true" : undefined}
      className={`rounded-full object-cover ${className ?? ""}`}
      {...rest}
    />
  );
}
