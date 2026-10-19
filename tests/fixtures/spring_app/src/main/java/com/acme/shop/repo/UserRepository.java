package com.acme.shop.repo;

import com.acme.shop.domain.User;
import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;

public interface UserRepository extends JpaRepository<User, Long> {
    List<User> findByEmailContaining(String part);

    @Query(value = "SELECT * FROM core.tb_users WHERE email_address = ?1", nativeQuery = true)
    User lookupNative(String email);
}
