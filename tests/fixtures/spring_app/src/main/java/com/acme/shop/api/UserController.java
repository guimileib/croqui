package com.acme.shop.api;

import com.acme.shop.config.ApiPaths;
import com.acme.shop.service.UserService;
import org.springframework.web.bind.annotation.*;

/** Users API. */
@RestController
@RequestMapping(ApiPaths.USERS)
public class UserController {

    private final UserService userService;

    public UserController(UserService userService) {
        this.userService = userService;
    }

    /**
     * Fetch one user.
     * <p>Returns 404 when missing.
     */
    @GetMapping("/{id:[0-9]+}")
    public UserDto get(@PathVariable Long id) {
        return userService.find(id);
    }

    @PostMapping
    public UserDto create(@RequestBody CreateUser body) {
        return userService.create(body);
    }

    @PutMapping(path = {"/{id}", "/legacy/{id}"})
    public UserDto update(@PathVariable("id") Long id, @RequestBody CreateUser body) {
        return userService.update(id, body);
    }

    @RequestMapping(method = RequestMethod.DELETE, value = "/{id}")
    public void remove(@PathVariable Long id) {
        userService.remove(id);
    }

    // @GetMapping("/commented-out")
    /* @PostMapping("/block-commented") */
    @GetMapping("/ping")
    public String ping() {
        String s = "@GetMapping(\"/in-string\") { }";
        return "pong";
    }
}
