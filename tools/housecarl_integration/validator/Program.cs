using System;
using System.IO;
using System.Runtime.CompilerServices;
using System.Runtime.Loader;
using HousecarlCore;
using Mutagen.Bethesda.Skyrim;

internal static class Program
{
    private static int Main(string[] args)
    {
        if (args.Length != 2)
            return 2;

        var server = Path.GetFullPath(args[0]);
        AssemblyLoadContext.Default.Resolving += (context, name) =>
        {
            var library = Path.Combine(server, name.Name + ".dll");
            return File.Exists(library) ? context.LoadFromAssemblyPath(library) : null;
        };

        try
        {
            Reopen(Path.GetFullPath(args[1]));
            Console.WriteLine("Readable");
            return 0;
        }
        catch (Exception error)
        {
            for (Exception current = error; current != null; current = current.InnerException)
                Console.Error.WriteLine($"{current.GetType().Name}: {current.Message}");
            return 1;
        }
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    private static void Reopen(string path)
    {
        // Use the same eager read as House Carl's in-place writer.
        _ = SkyrimMod.CreateFromBinary(path, SkyrimRelease.SkyrimSE, PluginTextEncoding.ReadFor(path));
    }
}
