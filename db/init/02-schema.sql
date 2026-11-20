-- Навчальна схема: невеликий інтернет-магазин.

CREATE TABLE customers (
    id          SERIAL PRIMARY KEY,
    name        TEXT        NOT NULL,
    email       TEXT        NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE products (
    id     SERIAL PRIMARY KEY,
    name   TEXT           NOT NULL,
    price  NUMERIC(10, 2) NOT NULL CHECK (price > 0),
    stock  INTEGER        NOT NULL DEFAULT 0
);

CREATE TABLE orders (
    id           BIGSERIAL PRIMARY KEY,
    customer_id  INTEGER        NOT NULL REFERENCES customers (id),
    product_id   INTEGER        NOT NULL REFERENCES products (id),
    quantity     INTEGER        NOT NULL CHECK (quantity > 0),
    total        NUMERIC(12, 2) NOT NULL,
    status       TEXT           NOT NULL DEFAULT 'new'
                 CHECK (status IN ('new', 'paid', 'shipped', 'cancelled')),
    created_at   TIMESTAMPTZ    NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ    NOT NULL DEFAULT now()
);

CREATE INDEX idx_orders_status   ON orders (status);
CREATE INDEX idx_orders_customer ON orders (customer_id);
CREATE INDEX idx_orders_created  ON orders (created_at);

INSERT INTO products (name, price, stock) VALUES
    ('Ноутбук',        32999.00, 500),
    ('Смартфон',       18999.00, 800),
    ('Навушники',       2499.00, 1500),
    ('Клавіатура',      1899.00, 1200),
    ('Миша',             799.00, 2000),
    ('Монітор',         9499.00, 600),
    ('Веб-камера',      1599.00, 900),
    ('Зарядний пристрій', 649.00, 3000),
    ('Powerbank',       1299.00, 1800),
    ('Флешка 128 ГБ',    399.00, 4000);

-- app_user отримує права лише на DML, без права змінювати структуру
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_user;
