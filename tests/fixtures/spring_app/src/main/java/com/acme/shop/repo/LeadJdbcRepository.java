package com.acme.shop.repo;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

@Repository
public class LeadJdbcRepository {
    private final JdbcTemplate jdbc;

    public LeadJdbcRepository(JdbcTemplate jdbc) { this.jdbc = jdbc; }

    public Object findByStatus(String status) {
        return jdbc.queryForList("""
            SELECT o.id, o.status
              FROM leads o
             WHERE o.status = ?
            """, status);
    }

    public void cancel(long id) {
        jdbc.update("UPDATE leads " +
                    "SET status = 'CANCELLED' WHERE id = ?", id);
    }
}
