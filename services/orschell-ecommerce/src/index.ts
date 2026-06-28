import 'dotenv/config';
import './db/database';
import express from 'express';
import cors from 'cors';
import authRouter from './routes/auth';
import productsRouter from './routes/products';
import categoriesRouter from './routes/categories';
import cartRouter from './routes/cart';
import ordersRouter from './routes/orders';
import adminRouter from './routes/admin';

const app = express();
const PORT = parseInt(process.env.PORT || '3742', 10);

app.use(cors({
  origin: [
    'https://michaelwegter.com',
    'https://www.michaelwegter.com',
    'http://localhost:5173',
    'http://localhost:4173',
    'http://localhost:3000',
  ],
  credentials: true,
}));
app.use(express.json());

app.get('/health', (_req, res) => res.json({ ok: true, service: 'orschell-ecommerce-api', ts: new Date().toISOString() }));

app.use('/auth',       authRouter);
app.use('/products',   productsRouter);
app.use('/categories', categoriesRouter);
app.use('/cart',       cartRouter);
app.use('/orders',     ordersRouter);
app.use('/admin',      adminRouter);

app.use((err: any, _req: express.Request, res: express.Response, _next: express.NextFunction) => {
  console.error('[error]', err);
  res.status(500).json({ error: 'Internal server error', detail: err.message });
});

app.listen(PORT, '127.0.0.1', () => {
  console.log(`[orschell-ecommerce-api] listening on http://127.0.0.1:${PORT}`);
});
