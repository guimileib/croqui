CREATE TABLE orders (
  id serial PRIMARY KEY,
  user_id integer NOT NULL,
  total numeric(12,2)
);
