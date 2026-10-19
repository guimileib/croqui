@RestController
@RequestMapping("/should-not-appear")
class IgnoredTest { @GetMapping("/x") void x() {} }
