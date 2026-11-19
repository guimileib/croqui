CREATE TABLE customers (
    id SERIAL PRIMARY KEY,
    email VARCHAR(200) NOT NULL,
    created_at TIMESTAMP
);

CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    total NUMERIC(12,2),
    CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers(id)
);

CREATE TABLE order_items (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER,
    quantity INTEGER
);

CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    sku VARCHAR(40)
);
