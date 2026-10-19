package com.acme.shop.repo;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

@Repository
public class OrderJdbcRepository {
    private final JdbcTemplate jdbc;

    public OrderJdbcRepository(JdbcTemplate jdbc) { this.jdbc = jdbc; }

    public Object findByStatus(String status) {
        return jdbc.queryForList("""
            SELECT o.id, o.status
              FROM orders o
             WHERE o.status = ?
            """, status);
    }

    public void cancel(long id) {
        jdbc.update("UPDATE orders " +
                    "SET status = 'CANCELLED' WHERE id = ?", id);
    }
}
