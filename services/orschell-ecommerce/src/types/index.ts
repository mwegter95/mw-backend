export interface AuthPayload {
  userId: number;
  email: string;
  name: string;
  role: string;
}

declare global {
  namespace Express {
    interface Request {
      user?: AuthPayload;
    }
  }
}

export interface ProductRow {
  id: number;
  category_id: number;
  name: string;
  slug: string;
  description: string;
  price: number;
  sku: string;
  image_url: string;
  specs_json: string | null;
  featured: number;
  active: number;
  created_at: string;
  category_name?: string;
  quantity?: number;
  low_stock_threshold?: number;
}

export interface CartItemRow {
  id: number;
  user_id: number;
  product_id: number;
  quantity: number;
  name?: string;
  price?: number;
  image_url?: string;
  sku?: string;
  stock?: number;
}

export interface OrderRow {
  id: number;
  user_id: number;
  status: string;
  subtotal: number;
  tax: number;
  total: number;
  shipping_name: string;
  shipping_address_json: string;
  payment_ref: string;
  created_at: string;
}

export interface OrderItemRow {
  id: number;
  order_id: number;
  product_id: number;
  quantity: number;
  unit_price: number;
  name?: string;
  sku?: string;
  image_url?: string;
}

export interface RunResult {
  changes: number;
  lastInsertRowid: number;
}
