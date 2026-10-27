// Command gotool prints a greeting.
package main

import (
	"fmt"

	"example.com/gotool/internal/store"
	"github.com/spf13/cobra"
)

func main() {
	fmt.Println(store.Greeting(), cobra.Command{})
}
