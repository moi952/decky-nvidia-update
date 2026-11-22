export type LaunchResult = {
  started: boolean;
  code: string | null;
  message: string | null;
};

export function launchErrorTitle(code: string | null): string {
  if (code === "already_installed") return "install_button_installed";
  if (code === "already_running") return "toast_already_running_title";
  if (code === "connection_lost" || code === "status_unavailable")
    return "operation_status_unknown";
  return "operation_request_failed";
}

export async function withTimeout<T>(
  request: Promise<T>,
  milliseconds = 30000,
): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      request,
      new Promise<never>((_, reject) => {
        timer = setTimeout(
          () => reject(new Error("Request timed out")),
          milliseconds,
        );
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

// Never retry a dispatch: a lost reply can belong to a successfully started job.
export async function requestOperation(
  request: () => Promise<LaunchResult>,
  recoverStatus: () => Promise<void>,
): Promise<LaunchResult> {
  let result: LaunchResult;
  try {
    result = await withTimeout(request());
  } catch {
    result = {
      started: false,
      code: "connection_lost",
      message:
        "Could not confirm whether the operation started. Reconnect to check its status before trying again.",
    };
  }
  if (!result.started) {
    try {
      await recoverStatus();
    } catch {
      // The caller keeps actions disabled until status is available again.
    }
  }
  return result;
}
