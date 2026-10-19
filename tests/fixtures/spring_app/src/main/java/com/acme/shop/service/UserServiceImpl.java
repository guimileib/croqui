package com.acme.shop.service;

import com.acme.shop.repo.UserRepository;
import org.springframework.stereotype.Service;

@Service
public class UserServiceImpl implements UserService {
    @org.springframework.beans.factory.annotation.Autowired
    private UserRepository userRepository;

    public Object find(Long id) { return userRepository.findById(id).orElseThrow(); }
    public Object create(Object body) { return userRepository.save(null); }
    public Object update(Long id, Object body) { return userRepository.save(null); }
    public void remove(Long id) { userRepository.deleteById(id); }
}
