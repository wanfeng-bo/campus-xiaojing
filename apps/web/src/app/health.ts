/** 表示后端和数据库均可用的最小公开健康响应。 */
export interface HealthResponse {
  status: "ok";
  database: "ok";
}


/** 在向页面交付数据前收窄未知响应，防止错误结构被当作健康状态。 */
function isHealthResponse(value: unknown): value is HealthResponse {
  return (
    typeof value === "object" &&
    value !== null &&
    "status" in value &&
    "database" in value &&
    value.status === "ok" &&
    value.database === "ok"
  );
}


/** 请求固定健康接口并校验响应结构，让页面只处理可信的状态。 */
export async function fetchHealth(fetcher: typeof fetch = fetch): Promise<HealthResponse> {
  const response = await fetcher("/api/v1/health", {
    headers: { Accept: "application/json" },
  });

  if (!response.ok) {
    throw new Error(`健康检查请求失败：${response.status}`);
  }

  const payload: unknown = await response.json();
  if (!isHealthResponse(payload)) {
    throw new Error("健康检查响应格式无效");
  }

  return payload;
}
