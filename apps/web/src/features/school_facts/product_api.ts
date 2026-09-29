import {
  isProductSession, isProductQuery, isProductProposal, isProductReceipt,
  type ProductSession, type ProductQuery, type ProductProposal, type ProductReceipt,
  type ProductPrepareInput, type ProductQuerySelection,
} from "./product_contracts";

export interface ProductApi {
  getSession(): Promise<ProductSession>;
  login(account_ref: string): Promise<ProductSession>;
  changeContext(context_ref: string): Promise<ProductSession>;
  logout(): Promise<ProductSession>;
  queryContacts(selection: string | ProductQuerySelection): Promise<ProductQuery>;
  prepareContact(input: ProductPrepareInput): Promise<ProductProposal>;
  confirmContact(proposal_ref: string): Promise<ProductReceipt>;
  getReceipt(proposal_ref: string): Promise<ProductReceipt>;
}

/** HTTP 失败仅保留固定文案，绝不回显未知服务端正文。 */
export class ProductApiError extends Error {
  constructor(readonly status: number, invalid = false) {
    super(invalid ? "返回的信息无法核验，请刷新后重试。" :
      status === 401 ? "当前身份已失效，请重新核对并选择当前身份。" :
      status === 403 ? "当前身份无权执行此操作。" : "学校事实服务暂时不可用，请稍后重试。");
  }
}

/** 仅调用产品窄接口；工程凭据由同源 HttpOnly cookie 管理。 */
export function createProductApi(): ProductApi {
  const base = "/api/v1/school-fact-product";
  async function request<T>(
    path: string, method: "GET" | "POST", decode: (value: unknown) => value is T,
    body?: Record<string, unknown>,
  ): Promise<T> {
    let response: Response;
    try {
      response = await fetch(base + path, {
        method, credentials: "same-origin",
        ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
      });
    } catch { throw new ProductApiError(0); }
    if (!response.ok) throw new ProductApiError(response.status);
    const value: unknown = await response.json().catch(() => undefined);
    if (!decode(value)) throw new ProductApiError(response.status, true);
    return value;
  }
  async function receipt(path: string, method: "GET" | "POST", ref: string) {
    const result = await request(path, method, isProductReceipt);
    if (result.reference !== ref) throw new ProductApiError(200, true);
    return result;
  }
  return {
    getSession: () => request("/session", "GET", isProductSession),
    login: (account_ref) => request("/login", "POST", isProductSession, { account_ref }),
    changeContext: (context_ref) => request("/context", "POST", isProductSession, { context_ref }),
    logout: () => request("/logout", "POST", isProductSession),
    queryContacts: (selection) => {
      const input = typeof selection === "string" ? { campus_name: selection } : selection;
      const { subject, scope, campus_name, campus_ref } = input;
      return request("/contacts/query", "POST", isProductQuery, {
        ...(subject === undefined ? {} : { subject }), ...(scope === undefined ? {} : { scope }),
        ...(campus_name === undefined ? {} : { campus_name }), ...(campus_ref === undefined ? {} : { campus_ref }),
      });
    },
    prepareContact: ({ contact_ref, contact_value, service_hours, reason }) =>
      request("/contacts/prepare", "POST", isProductProposal, { contact_ref, contact_value, service_hours, reason }),
    confirmContact: (ref) => receipt(`/contacts/proposals/${encodeURIComponent(ref)}/confirm`, "POST", ref),
    getReceipt: (ref) => receipt(`/receipts/${encodeURIComponent(ref)}`, "GET", ref),
  };
}
