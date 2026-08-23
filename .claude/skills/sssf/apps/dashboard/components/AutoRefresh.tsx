"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

const REFRESH_INTERVAL_MS = 3_000;

/**
 * Poll the server component tree while a run is in flight.
 * Renders nothing; the interval stops as soon as isInFlight turns false.
 */
export function AutoRefresh({ isInFlight }: { isInFlight: boolean }) {
  const router = useRouter();

  useEffect(() => {
    if (!isInFlight) return;
    const id = setInterval(() => router.refresh(), REFRESH_INTERVAL_MS);
    return () => clearInterval(id);
  }, [isInFlight, router]);

  return null;
}
